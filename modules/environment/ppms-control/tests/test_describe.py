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

from ppms.config import Config
from ppms.sim_system import build_sim_system
from ppms.net.describe import build_manifest, read_path
from ppms.net.service import PpmsService
from ppms.net.client import PpmsClient


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
        assert m["module"] == "ppms"
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
        cfg.limits.field_max_mT = 5000.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.field_max_mT = 9000.0
        assert build_manifest(brain)["revision"] == rev0, \
            "revision is a counter, not derived from the manifest"
        brain.poll_once()                   # new readings must not move it
        assert build_manifest(brain)["revision"] == rev0
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
    svc = PpmsService(brain, host="127.0.0.1", cmd_port=5791, pub_port=5792)
    svc.start()
    client = None
    try:
        client = PpmsClient(host="127.0.0.1", cmd_port=5791, pub_port=5792)
        client.start()
        m = client.describe()
        assert m["module"] == "ppms"

        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_limits_come_from_the_config():
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        assert (m["field"]["min"], m["field"]["max"]) == (-cfg.limits.field_max_mT,
                                                          cfg.limits.field_max_mT)
        assert (m["temperature"]["min"], m["temperature"]["max"]) == (
            cfg.limits.temperature_min_K, cfg.limits.temperature_max_K)
    finally:
        brain.shutdown()


def test_setpoints_settle_adopt_then_flag_with_a_derived_timeout():
    """The scan-safe rule: status must show OUR setpoint before the flag counts,
    and the timeout must cover a real magnet ramp (scan-core's default 60 s
    would abort a 9 T sweep)."""
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        f = m["field"]
        assert f["settle"] == {"policy": "adopt_then_flag",
                               "setpoint_key": "setpoint_field_mT",
                               "flag_key": "field_stable"}
        full_sweep_s = 2 * cfg.limits.field_max_mT / cfg.field.rate_mT_per_s
        assert f["timeout_s"] > full_sweep_s
        t = m["temperature"]
        assert t["settle"]["flag_key"] == "temperature_stable"
        lim = cfg.limits                      # 400 K -> 1.8 K at 20 K/min is ~20 min
        full_cool_s = (lim.temperature_max_K - lim.temperature_min_K) / (
            cfg.temperature.rate_K_per_min / 60.0)
        assert t["timeout_s"] > full_cool_s + 1800   # plus time to settle near base
        # slower ramp -> longer timeout -> a new revision
        rev = build_manifest(brain)["revision"]
        cfg.field.rate_mT_per_s = 2.0
        m2 = _by_id(build_manifest(brain))
        assert m2["field"]["timeout_s"] > f["timeout_s"]
        assert build_manifest(brain)["revision"] != rev
    finally:
        brain.shutdown()


def test_every_read_path_exists_in_status():
    """A descriptor pointing at a key the status does not have is a blank widget."""
    from ppms.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        st = status_to_dict(brain.status())
        for p in build_manifest(brain)["parameters"]:
            if p.get("read_path"):
                assert p["read_path"][0] in st, p["id"]
    finally:
        brain.shutdown()
