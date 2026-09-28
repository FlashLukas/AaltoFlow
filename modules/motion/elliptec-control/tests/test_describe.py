"""Tests for the `describe` manifest -- this service's self-description.

Two properties matter most and are easy to get wrong:

  * every bound is read LIVE from cfg, never copied into a literal;
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes.
"""

import time

import pytest

pytest.importorskip("zmq")

from elliptec.config import Config
from elliptec.net.client import ElliptecClient
from elliptec.net.describe import build_manifest, read_path
from elliptec.net.service import ElliptecService
from elliptec.sim_system import build_sim_system


def _brain(addresses="0"):
    cfg = Config()
    cfg.axes.addresses = addresses
    cfg.sim.max_speed_deg_s = 900.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    return cfg, brain


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    _cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "elliptec"
        assert isinstance(m["revision"], int)
        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"
        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
                assert "settle" in p, p["id"]
            if p["kind"] == "indicator":
                assert p["read_path"], p["id"]
    finally:
        brain.shutdown()


def test_angle_control_is_honest():
    cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))["angle_0"]
        assert (p["min"], p["max"]) == (cfg.limits.min_angle_deg, cfg.limits.max_angle_deg)
        assert p["unit"] == "deg"
        assert p["set"] == {"verb": "move_abs", "arg": "angle_deg", "extra": {"axis": 0}}
        s = p["settle"]
        assert s["policy"] == "adopt_then_flag" and s["index"] == 0
        assert (s["setpoint_key"], s["flag_key"], s["invert"]) == ("target_deg", "moving", True)
        v = _by_id(build_manifest(brain))["velocity_0"]
        assert (v["min"], v["max"]) == (cfg.limits.min_velocity_pct, cfg.limits.max_velocity_pct)
        assert v["settle"] == {"policy": "echoes", "key": "velocity_pct", "index": 0}
    finally:
        brain.shutdown()


def test_every_read_path_resolves_against_real_status():
    _cfg, brain = _brain("0,1")
    try:
        st = brain.status_dict()
        for p in build_manifest(brain)["parameters"]:
            if p.get("read_path"):
                assert read_path(st, p["read_path"]) is not None or p["id"].startswith("error"), p["id"]
        assert read_path({}, ["angle_deg", 0]) is None     # must not raise
    finally:
        brain.shutdown()


def test_one_set_of_descriptors_per_address():
    _cfg, brain = _brain("0,B")
    try:
        ids = _by_id(build_manifest(brain))
        for s, i in (("0", 0), ("b", 1)):
            assert ids[f"angle_{s}"]["set"]["extra"]["axis"] == i
            assert ids[f"angle_{s}"]["settle"]["index"] == i
            assert f"home_{s}" in ids and f"set_zero_{s}" in ids
    finally:
        brain.shutdown()


def test_revision_tracks_bounds_but_not_values():
    cfg, brain = _brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        brain.move_abs(0, 100.0)             # a value change
        time.sleep(0.1)
        assert build_manifest(brain)["revision"] == rev0
        cfg.limits.max_angle_deg = 180.0     # a bound change
        assert build_manifest(brain)["revision"] != rev0
        cfg.limits.max_angle_deg = 360.0
        assert build_manifest(brain)["revision"] == rev0, "revision must be derived, not counted"
    finally:
        brain.shutdown()


def test_actions_scan_routines_can_wait_on():
    _cfg, brain = _brain()
    try:
        ids = _by_id(build_manifest(brain))
        home = ids["home_0"]
        assert home["wait"]["target_key"] == "move_id"
        assert home["wait"]["ready"]["setpoint_key"] == "move_id"
        assert home["wait"]["ready"]["index"] == 0
        assert ids["stop"]["danger"] is True
    finally:
        brain.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    _cfg, brain = _brain()
    svc = ElliptecService(brain, host="127.0.0.1", cmd_port=17304, pub_port=17305)
    svc.start()
    client = None
    try:
        client = ElliptecClient(host="127.0.0.1", cmd_port=17304, pub_port=17305)
        client.start()
        m = client.describe()
        assert m["module"] == "elliptec"
        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"]
        # the home action as scan-core would run it: send the id, wait on move_id
        r = client._rpc(cmd="home_0")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            st = client._rpc(cmd="status")["status"]
            if st["move_id"][0] == r["move_id"] and not st["moving"][0]:
                break
            time.sleep(0.05)
        assert st["homed"][0]
    finally:
        if client is not None:
            client.close()
        svc.stop()
