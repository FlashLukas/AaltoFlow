"""Tests for the `describe` manifest -- this service's self-description.

Two properties matter most and are easy to get wrong:

  * every bound is read LIVE from cfg or from status, never copied into a
    literal (a manifest that restates a limit is a limit with two homes, and
    the wrong one does not announce itself -- it just draws a slider with the
    wrong range)
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes, or clients either cache a stale panel or re-fetch
    several times a second
"""

import pytest

pytest.importorskip("zmq")

from tc200.config import Config
from tc200.sim_system import build_sim_system
from tc200.net.describe import build_manifest, read_path
from tc200.net.service import Tc200Service
from tc200.net.client import Tc200Client


def _brain():
    cfg = Config()
    built = build_sim_system(cfg)
    brain = built[0] if isinstance(built, tuple) else built
    brain.start()
    return cfg, brain


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "tc200"
        assert isinstance(m["revision"], int)
        assert m["parameters"], "manifest is empty"

        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"

        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            assert p["type"] in ("float", "int", "bool", "enum", "string",
                                 "action"), p["id"]
            # a control a client cannot drive, or an indicator it cannot read,
            # is dead weight in a control screen
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            if p["kind"] == "indicator":
                assert p["read_path"], p["id"]
            if p["kind"] == "action" and p.get("args"):
                for a in p["args"]:
                    assert "name" in a and "type" in a, p["id"]
    finally:
        brain.shutdown()


def test_revision_tracks_bounds_but_not_values():
    cfg, brain = _brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        cfg.limits.temperature_max_C = 60.0
        assert build_manifest(brain)["revision"] != rev0,             "revision ignored a limit change"
        cfg.limits.temperature_max_C = 100.0
        assert build_manifest(brain)["revision"] == rev0,             "revision is a counter, not derived from the manifest"
        brain.poll_once()                   # new readings must not move it
        assert build_manifest(brain)["revision"] == rev0
    finally:
        brain.shutdown()


def test_ceiling_follows_the_box_tmax_and_moves_the_revision():
    """The dynamic limit of this module: TMAX lives in the TC200."""
    cfg, brain = _brain()
    try:
        m0 = build_manifest(brain)
        t0 = _by_id(m0)["temperature"]
        assert t0["max"] == min(cfg.limits.temperature_max_C,
                                brain.status().tmax_C - cfg.limits.tmax_margin_C)
        brain.set_tmax(60.0)
        m1 = build_manifest(brain)
        assert _by_id(m1)["temperature"]["max"] == 60.0 - cfg.limits.tmax_margin_C
        assert m1["revision"] != m0["revision"]
    finally:
        brain.shutdown()


def test_read_path_resolves_and_survives_a_missing_branch():
    cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        ind = [p for p in m["parameters"] if p["kind"] == "indicator"][0]
        assert read_path({}, ind["read_path"]) is None   # must not raise
    finally:
        brain.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently.

    A client uses the REQ reply whenever no PUB frame has arrived yet, since
    ZeroMQ SUB is a slow joiner.
    """
    cfg, brain = _brain()
    svc = Tc200Service(brain, host="127.0.0.1", cmd_port=17362, pub_port=17363)
    svc.start()
    client = None
    try:
        client = Tc200Client(host="127.0.0.1", cmd_port=17362, pub_port=17363)
        client.start()
        m = client.describe()
        assert m["module"] == "tc200"

        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"],             "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_limits_come_from_the_config():
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        assert m["temperature"]["min"] == cfg.limits.temperature_min_C
        assert m["pmax"]["max"] == cfg.limits.pmax_max_W
        cfg.limits.pmax_max_W = 5.0
        assert _by_id(build_manifest(brain))["pmax"]["max"] == 5.0
    finally:
        brain.shutdown()


def test_setpoint_settles_adopt_then_flag_with_a_derived_timeout():
    """The scan-safe rule: status must show OUR setpoint before the flag counts,
    and the timeout must cover a slow heat-up / passive cool-down (scan-core's
    default 60 s would abort almost every point)."""
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        t = m["temperature"]
        assert t["settle"] == {"policy": "adopt_then_flag",
                               "setpoint_key": "setpoint_C",
                               "flag_key": "temperature_stable"}
        span = t["max"] - t["min"]
        assert t["timeout_s"] > span / (cfg.temperature.slowest_rate_C_per_min / 60.0)
        rev = build_manifest(brain)["revision"]
        cfg.temperature.slowest_rate_C_per_min = 0.5
        m2 = _by_id(build_manifest(brain))
        assert m2["temperature"]["timeout_s"] > t["timeout_s"]
        assert build_manifest(brain)["revision"] != rev
    finally:
        brain.shutdown()


def test_safety_flags_and_scan_action():
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        assert m["sensor"].get("danger") is True
        assert m["tmax"].get("danger") is True
        assert m["sensor"]["options"] == ["ptc100", "ptc1000", "th10k"]
        # the heater-off action is usable in a scan routine
        assert m["heater_off"]["kind"] == "action"
        assert m["heater_off"]["wait"] == {"ready": {"policy": "immediate"}}
        assert m["enabled"]["settle"] == {"policy": "echoes", "key": "enabled"}
    finally:
        brain.shutdown()


def test_every_read_path_exists_in_status():
    """A descriptor pointing at a key the status does not have is a blank widget."""
    from tc200.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        st = status_to_dict(brain.status())
        for p in build_manifest(brain)["parameters"]:
            if p.get("read_path"):
                assert p["read_path"][0] in st, p["id"]
    finally:
        brain.shutdown()
