"""Tests for the `describe` manifest -- this service's self-description.

What matters most:
  * every bound is read LIVE (the gain ceiling is the intersection of the
    device range and the safety limit), never copied into a literal;
  * `revision` changes when the bounds change, and not when a value changes;
  * the settle policies can actually be satisfied (an echo tolerance smaller
    than half a gain step would make scan-core wait out its whole timeout on a
    point that had arrived).
"""

import pytest

pytest.importorskip("zmq")

from dsamp.config import Config
from dsamp.net.client import DsampClient
from dsamp.net.describe import build_manifest, read_path
from dsamp.net.protocol import status_to_dict
from dsamp.net.service import DsampService
from dsamp.sim_system import build_sim_system


@pytest.fixture
def brain():
    cfg = Config()
    amp, _ = build_sim_system(cfg)
    amp.start()
    yield amp
    amp.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    m = build_manifest(brain)
    assert m["module"] == "dsamp"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    st = status_to_dict(brain.status())
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert "settle" in p, p["id"]
        if p["kind"] in ("control", "indicator"):
            assert p["read_path"], p["id"]
            # every read_path resolves against a REAL status dict
            assert read_path(st, p["read_path"]) is not None, p["id"]
        if p["kind"] == "action":
            assert "wait" in p, p["id"]


def test_gain_limits_are_the_live_envelope(brain):
    g = _by_id(build_manifest(brain))["gain"]
    assert (g["min"], g["max"]) == brain.gain_range()
    assert g["max"] == brain.cfg.limits.gain_max_dB     # the ceiling wins by default
    brain.cfg.limits.gain_max_dB = 99.0                  # now the device maximum wins
    g = _by_id(build_manifest(brain))["gain"]
    assert g["max"] == brain.cfg.hardware.gain_max_dB


def test_revision_tracks_bounds_but_not_values(brain):
    rev0 = build_manifest(brain)["revision"]
    brain.set_gain(5.0)
    brain.poll_once()
    assert build_manifest(brain)["revision"] == rev0, "a value change moved the revision"
    brain.cfg.limits.gain_max_dB = 6.0
    assert build_manifest(brain)["revision"] != rev0, "revision ignored a ceiling change"
    brain.cfg.limits.gain_max_dB = Config().limits.gain_max_dB
    assert build_manifest(brain)["revision"] == rev0, "revision is not derived"


def test_gain_settle_tolerance_absorbs_the_step(brain):
    g = _by_id(build_manifest(brain))["gain"]
    assert g["settle"]["policy"] == "echoes"
    assert g["settle"]["key"] == "gain_dB"
    step = brain.cfg.hardware.gain_step_dB
    assert g["settle"]["tol"] >= step / 2.0
    assert g["step"] == step
    # the settle must be reachable: ask for 6.2, the device holds 6.0
    brain.set_gain(6.2)
    brain.poll_once()
    assert abs(brain.status().gain_dB - 6.2) <= g["settle"]["tol"]


def test_amp_on_is_flagged_dangerous_and_amp_off_is_a_routine_action(brain):
    m = _by_id(build_manifest(brain))
    assert m["amp_on"].get("danger") is True
    assert m["amp_off"]["kind"] == "action"
    # the routine waits for the device's own "off" readback, not just the reply
    ready = m["amp_off"]["wait"]["ready"]
    assert ready == {"policy": "flag_only", "key": "amp_on", "invert": True}


def test_frequency_is_offered_in_MHz_with_a_scale(brain):
    f = _by_id(build_manifest(brain))["frequency"]
    assert f["unit"] == "MHz" and f["scale"] == 1e6
    assert f["max"] == pytest.approx(brain.cfg.limits.freq_max_Hz / 1e6)
    assert f["read_path"] == ["frequency_Hz"]


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently."""
    cfg = Config()
    amp, _ = build_sim_system(cfg)
    svc = DsampService(amp, host="127.0.0.1", cmd_port=17150, pub_port=17151)
    svc.start()
    client = None
    try:
        client = DsampClient(host="127.0.0.1", cmd_port=17150, pub_port=17151)
        client.start()
        m = client.describe()
        assert m["module"] == "dsamp"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        # every action id is a verb the service knows
        for p in m["parameters"]:
            if p["kind"] == "action":
                assert client._cmd({"cmd": p["id"]})["ok"], p["id"]
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()
