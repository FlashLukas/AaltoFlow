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

from shsg.config import Config
from shsg.sim_system import build_sim_system
from shsg.net.describe import build_manifest, read_path
from shsg.net.service import ShsgService
from shsg.net.client import ShsgClient


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
        assert m["module"] == "shsg"
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
        original = cfg.limits.power_max_dBm
        cfg.limits.power_max_dBm = -15.0
        assert build_manifest(brain)["revision"] != rev0, \
            "revision ignored a limit change"
        cfg.limits.power_max_dBm = original
        assert build_manifest(brain)["revision"] == rev0, \
            "revision is a counter, not derived from the manifest"
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
    svc = ShsgService(brain, host="127.0.0.1", cmd_port=17652, pub_port=17653)
    svc.start()
    client = None
    try:
        client = ShsgClient(host="127.0.0.1", cmd_port=17652, pub_port=17653)
        client.start()
        m = client.describe()
        assert m["module"] == "shsg"

        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"], \
            "the status command reply and the manifest disagree"
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_power_limits_come_from_the_config():
    cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))["power"]
        assert (p["min"], p["max"]) == (cfg.limits.power_min_dBm,
                                        cfg.limits.power_max_dBm)
    finally:
        brain.shutdown()


def test_frequency_is_offered_in_MHz_with_a_scale():
    """Scanned in MHz, commanded and published in Hz -- one scale, both ways."""
    cfg, brain = _brain()
    try:
        f = _by_id(build_manifest(brain))["frequency"]
        assert f["unit"] == "MHz"
        assert f["scale"] == 1e6
        assert f["max"] == pytest.approx(cfg.limits.freq_max_Hz / 1e6)
        assert f["read_path"] == ["frequency_Hz"]
    finally:
        brain.shutdown()


# ---- the settle rule: echo of OUR value, and only while the TG is ready -----

def _settled(block, status, target):
    """What scan-core's adopt_then_flag does with a settle block (a local copy,
    so this module's tests do not import scan-core): the setpoint key must echo
    the target within tol, and only THEN is the flag believed."""
    assert block["policy"] == "adopt_then_flag"
    v = status.get(block["setpoint_key"])
    if v is None or abs(float(v) - float(target)) > block.get("tol", 1e-6):
        return False
    flag = bool(status.get(block["flag_key"]))
    return (not flag) if block.get("invert") else flag


def test_controls_settle_on_the_echo_while_the_tg_is_ready():
    from shsg.net.protocol import status_to_dict
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        f = m["frequency"]["settle"]
        assert f["setpoint_key"] == "frequency_Hz" and f["flag_key"] == "tg_ready"
        assert f["tol"] == cfg.hardware.echo_tol_Hz          # read live from cfg
        assert m["power"]["settle"]["tol"] == cfg.hardware.echo_tol_dB
        assert m["rf_on"]["settle"]["setpoint_key"] == "rf_on"

        brain.set_frequency(2e9)
        st = status_to_dict(brain.status())
        assert _settled(f, st, 2e9)
        assert not _settled(f, st, 3e9), "accepted a value that is not echoed"

        # an SNA sweep holds the TG: the echo still matches, but no CW comes out
        brain.backend.simulate_sweep(True)
        st = status_to_dict(brain.status())
        assert st["frequency_Hz"] == 2e9 and st["tg_busy"] is True
        assert not _settled(f, st, 2e9), "settled while an SNA sweep holds the TG"
        brain.backend.simulate_sweep(False)

        # the TG state unreadable: its numbers mean nothing
        brain.backend.simulate_unknown()
        assert not _settled(f, status_to_dict(brain.status()), 2e9)
    finally:
        brain.shutdown()


def test_status_flags_are_described_as_indicators():
    cfg, brain = _brain()
    try:
        m = _by_id(build_manifest(brain))
        for pid in ("tg_busy", "tg_unknown", "tg_ready", "hw_error", "connected",
                    "parked", "park_Hz", "park_dBm"):
            assert m[pid]["kind"] == "indicator", pid
        assert "phase" not in m, "the TG has no phase"
    finally:
        brain.shutdown()
