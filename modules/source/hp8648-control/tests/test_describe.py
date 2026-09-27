"""Tests for the `describe` manifest -- this service's self-description.

What matters most and is easy to get wrong:

  * every bound is read LIVE (cfg, spec.py, the brain), never copied into a
    literal -- here especially the power ceiling, which follows the frequency
  * `revision` changes when the STRUCTURE or the BOUNDS change (crossing
    2500 MHz), and not when a measured value changes
  * the settle tolerances accept the instrument's own rounding, or a scan
    asking for -12.34 dBm would wait forever for an echo of -12.3
"""

import time

import pytest

pytest.importorskip("zmq")

from hp8648 import spec
from hp8648.config import Config
from hp8648.sim_system import build_sim_system
from hp8648.net.describe import build_manifest, read_path
from hp8648.net.protocol import status_to_dict
from hp8648.net.service import Hp8648Service
from hp8648.net.client import Hp8648Client

CMD_PORT = 17422
PUB_PORT = 17423


@pytest.fixture
def brain():
    cfg = Config()
    cfg.hardware.switch_settle_s = 0.0
    src, sim = build_sim_system(cfg)
    src.start()
    yield src
    src.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    m = build_manifest(brain)
    assert m["module"] == "hp8648"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    assert {"rf_on", "frequency", "power", "rpp_tripped"} <= set(ids)
    assert "phase" not in ids                       # the 8648 has no phase control
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert p["settle"]["policy"] == "echoes", p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_every_read_path_resolves_in_a_real_status(brain):
    st = status_to_dict(brain.status())
    for p in build_manifest(brain)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]
    assert read_path({}, ["rf_on"]) is None          # must not raise


def test_every_settle_key_is_a_status_key(brain):
    st = status_to_dict(brain.status())
    for p in build_manifest(brain)["parameters"]:
        if "settle" in p:
            assert p["settle"]["key"] in st, p["id"]


def test_power_max_is_the_live_ceiling_and_revision_follows(brain):
    brain.set_frequency(2.0e9)
    m1 = build_manifest(brain)
    assert _by_id(m1)["power"]["max"] == 13.0
    brain.set_frequency(2.2e9)                      # same band: nothing changes
    assert build_manifest(brain)["revision"] == m1["revision"]
    brain.set_frequency(3.0e9)                      # crosses 2500 MHz
    m2 = build_manifest(brain)
    assert _by_id(m2)["power"]["max"] == 10.0
    assert m2["revision"] != m1["revision"]


def test_revision_ignores_values(brain):
    rev0 = build_manifest(brain)["revision"]
    brain.set_power(-50.0)
    brain.set_rf(True)
    brain.wait_idle()
    assert build_manifest(brain)["revision"] == rev0


def test_limits_come_from_the_config(brain):
    brain.cfg.limits.power_max_dBm = 3.0
    brain.cfg.limits.freq_max_Hz = 1e9
    d = _by_id(build_manifest(brain))
    assert d["power"]["max"] == 3.0
    assert d["frequency"]["max"] == pytest.approx(1000.0)
    assert d["power"]["min"] == brain.cfg.limits.power_min_dBm


def test_frequency_is_offered_in_MHz_with_a_scale(brain):
    f = _by_id(build_manifest(brain))["frequency"]
    assert f["unit"] == "MHz" and f["scale"] == 1e6
    assert f["read_path"] == ["frequency_Hz"]
    assert f["settle"]["tol"] == spec.FREQ_RESOLUTION_HZ


def test_echo_tolerance_accepts_the_instrument_rounding(brain):
    """The check scan-core's `echoes` policy makes, with the declared tol."""
    tol = _by_id(build_manifest(brain))["power"]["settle"]["tol"]
    brain.set_power(-12.34)
    brain.wait_idle()
    assert abs(brain.status().power_dBm - (-12.34)) <= tol
    ftol = _by_id(build_manifest(brain))["frequency"]["settle"]["tol"]
    brain.set_frequency(1_234_567_894.0)
    brain.wait_idle()
    assert abs(brain.status().frequency_Hz - 1_234_567_894.0) <= ftol


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    """A field in only one status path is a field that vanishes intermittently."""
    svc = Hp8648Service(brain, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                        status_hz=20.0)
    svc.start()
    client = Hp8648Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    try:
        client.start()
        m = client.describe()
        assert m["module"] == "hp8648"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        time.sleep(0.3)
        with client._lock:
            published = dict(client._latest)
        assert set(published) == set(direct)
    finally:
        client.shutdown()
        svc._stop.set()
        time.sleep(0.4)


def test_every_set_verb_is_served_and_echoes_like_scan_core(brain):
    """Walk each control the way scan-core does: send value*scale under the
    declared verb/arg, then apply the declared echo check to the status the
    service publishes. A verb or arg typo, or an echo that can never match,
    fails here instead of on the rig (reviewer addition)."""
    svc = Hp8648Service(brain, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    targets = {"rf_on": True, "frequency": 2345.6789, "power": -21.37}
    controls = [p for p in build_manifest(brain)["parameters"] if p["kind"] == "control"]
    assert {p["id"] for p in controls} == set(targets)
    for p in controls:
        value = targets[p["id"]]
        wire = value if p["type"] == "bool" else value * p.get("scale", 1.0)
        reply = svc._dispatch({"cmd": p["set"]["verb"], p["set"]["arg"]: wire})
        assert reply == {"ok": True}, (p["id"], reply)
        brain.wait_idle()
        st = svc.status_payload()
        settle = p["settle"]
        assert settle["policy"] == "echoes"
        got = read_path(st, [settle["key"]])
        assert abs(float(got) - float(wire)) <= settle.get("tol", 1e-6), (p["id"], got, wire)
