"""Tests for the `describe` manifest -- this service's self-description.

What matters most here:
  * every bound is read LIVE from cfg, never a literal
  * the SHAPE follows the mode (current <-> voltage swap which knob is the
    output and which is the limit), and `revision` changes with it
  * `revision` does NOT change when a measured value changes
  * the settle rules are ones a scan can actually wait on
"""

import time

import pytest

pytest.importorskip("zmq")

from kepco.config import Config
from kepco.sim_system import build_sim_system
from kepco.net.describe import build_manifest, read_path
from kepco.net.service import KepcoService
from kepco.net.client import KepcoClient

CMD_PORT, PUB_PORT = 17010, 17011


@pytest.fixture
def brain():
    cfg = Config()
    supply, _ = build_sim_system(cfg, seed=0)
    supply.start(poll=False)
    yield supply
    supply.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    m = build_manifest(brain)
    assert m["module"] == "kepco"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert "settle" in p, p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_the_shape_follows_the_mode(brain):
    cur = _by_id(build_manifest(brain))
    assert "current" in cur and "voltage_limit" in cur
    assert "voltage" not in cur and "current_limit" not in cur
    rev_cur = build_manifest(brain)["revision"]

    brain.set_mode("voltage")
    volt = _by_id(build_manifest(brain))
    assert "voltage" in volt and "current_limit" in volt
    assert "current" not in volt and "voltage_limit" not in volt
    assert build_manifest(brain)["revision"] != rev_cur


def test_bounds_come_from_the_config(brain):
    cfg = brain.cfg
    p = _by_id(build_manifest(brain))
    assert (p["current"]["min"], p["current"]["max"]) == (cfg.limits.current_min_A,
                                                          cfg.limits.current_max_A)
    assert p["voltage_limit"]["max"] == max(abs(cfg.limits.voltage_min_V),
                                            abs(cfg.limits.voltage_max_V))
    rev0 = build_manifest(brain)["revision"]
    cfg.limits.current_max_A = 3.0
    p = _by_id(build_manifest(brain))
    assert p["current"]["max"] == 3.0
    assert build_manifest(brain)["revision"] != rev0
    cfg.limits.current_max_A = 10.0
    assert build_manifest(brain)["revision"] == rev0, "revision must be derived"


def test_revision_ignores_measured_values(brain):
    rev0 = build_manifest(brain)["revision"]
    brain.set_current(1.0)
    brain.set_output(True)
    for _ in range(5):
        brain.step(dt=0.05)
    assert build_manifest(brain)["revision"] == rev0


def test_ramped_setpoint_settles_on_adopt_then_ramping(brain):
    p = _by_id(build_manifest(brain))["current"]
    s = p["settle"]
    assert s["policy"] == "adopt_then_flag"
    assert s["setpoint_key"] == "current_set_A" == p["read_path"][0]
    assert s["flag_key"] == "ramping" and s["invert"] is True


def test_detectors_are_acquired_with_a_numbered_trigger(brain):
    p = _by_id(build_manifest(brain))
    for pid in ("measured_voltage", "measured_current"):
        acq = p[pid]["acquire"]
        assert acq["trigger_verb"] == "acquire"
        assert acq["target_key"] == "acq_id"               # gotcha #17
        assert acq["ready"]["setpoint_key"] == "acq_id"
        assert p[pid]["read_path"][0] == "sample"
    assert p["measured_voltage"]["acquire"]["group"] == p["measured_current"]["acquire"]["group"]
    assert "wait" in p["acquire"]                          # usable in a routine
    assert p["output_off_now"].get("danger") is True


def test_read_path_resolves_and_survives_a_missing_branch(brain):
    m = build_manifest(brain)
    for p in m["parameters"]:
        if p["read_path"]:
            read_path({}, p["read_path"])                  # must not raise
    assert read_path({"sample": {}}, ["sample", "voltage_V"]) is None


def test_describe_over_the_wire_and_both_status_paths_agree():
    cfg = Config()
    supply, _ = build_sim_system(cfg, seed=0)
    svc = KepcoService(supply, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    svc.start()
    client = None
    try:
        client = KepcoClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
        client.start()
        m = client.describe()
        assert m["module"] == "kepco"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
        # a mode change must reach clients through describe_rev
        client.set_mode("voltage")
        time.sleep(0.3)
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] != m["revision"]
        assert direct["describe_rev"] == client.describe()["revision"]
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()
