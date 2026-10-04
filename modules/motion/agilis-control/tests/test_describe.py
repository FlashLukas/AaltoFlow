"""Tests for the `describe` manifest -- this service's self-description.

  * every bound is read LIVE from cfg or status, never copied into a literal
  * `revision` changes when the STRUCTURE or the BOUNDS change, and not when a
    measured value changes
"""

import time
from dataclasses import asdict

import pytest

pytest.importorskip("zmq")

from agilis.config import Config
from agilis.net.client import AgilisClient
from agilis.net.describe import build_manifest, read_path
from agilis.net.service import AgilisService
from agilis.sim_system import build_sim_system

CMD, PUB = 17170, 17171


def _brain():
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    brain.start()
    return cfg, brain


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def test_manifest_shape_and_required_fields():
    _cfg, brain = _brain()
    try:
        m = build_manifest(brain)
        assert m["module"] == "agilis" and isinstance(m["revision"], int)
        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids))
        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
                assert "settle" in p, p["id"]
            if p["kind"] == "indicator":
                assert p["read_path"], p["id"]
            if p["kind"] == "action":
                # immediate (datum, stop) or a numbered routine (limit switch)
                w = p["wait"]
                assert w["ready"]["policy"] in ("immediate", "adopt_then_flag"), p["id"]
                if w["ready"]["policy"] == "adopt_then_flag":
                    assert w["target_key"] == "routine_id"
                    assert w["check"] == {"key": "routine_error", "equals": "OK"}
        assert {"position_x", "position_y"} <= set(ids)
        assert not any(i.endswith("_z") for i in ids)          # two axes only
    finally:
        brain.shutdown()


def test_every_read_path_resolves_against_a_real_status():
    _cfg, brain = _brain()
    try:
        st = asdict(brain.status())
        for p in build_manifest(brain)["parameters"]:
            if p.get("read_path"):
                if p["id"].startswith("measured_um"):
                    # None until MA/PA/step-size has run: the key must exist
                    assert p["read_path"][0] in st, p["id"]
                    continue
                assert read_path(st, p["read_path"]) is not None, p["id"]
        assert read_path({}, ["position_um", 0]) is None      # must not raise
    finally:
        brain.shutdown()


def test_every_set_verb_and_action_exists_on_the_service():
    """A control whose verb the service does not know is a dead button."""
    _cfg, brain = _brain()
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD + 4, pub_port=PUB + 4)
    try:
        for p in build_manifest(brain)["parameters"]:
            if p["kind"] == "control":
                s = p["set"]
                value = 16 if "amplitude" in p["id"] else (False if p["type"] == "bool" else 1.0)
                reply = svc._dispatch({"cmd": s["verb"], s["arg"]: value, **s.get("extra", {})})
            elif p["kind"] == "action":
                reply = svc._dispatch({"cmd": p["id"]})
                if "routine_id" in reply:
                    # a routine started: abort it (MA ends by itself), wait
                    brain._abort.set()
                    t0 = time.monotonic()
                    while brain._routine_running and time.monotonic() - t0 < 5:
                        time.sleep(0.02)
            else:
                continue
            assert reply["ok"], (p["id"], reply)
            time.sleep(0.05)
    finally:
        brain.shutdown()


def test_revision_tracks_bounds_but_not_values():
    _cfg, brain = _brain()
    try:
        rev0 = build_manifest(brain)["revision"]
        brain.move_steps(0, 500)
        time.sleep(0.2)
        assert build_manifest(brain)["revision"] == rev0, "a value moved the revision"
        brain.set_leash(True, leash_steps=1000)
        time.sleep(0.1)
        assert build_manifest(brain)["revision"] != rev0
        brain.set_leash(False)
        time.sleep(0.1)
        assert build_manifest(brain)["revision"] == rev0
    finally:
        brain.shutdown()


def test_armed_leash_narrows_the_position_bounds():
    _cfg, brain = _brain()
    try:
        w = _by_id(build_manifest(brain))["position_x"]
        brain.set_leash(True, leash_steps=1000)
        time.sleep(0.1)
        t = _by_id(build_manifest(brain))["position_x"]
        assert (t["min"], t["max"]) == pytest.approx((-50.0, 50.0))   # 1000 x 50 nm
        assert (t["max"] - t["min"]) < (w["max"] - w["min"])
        assert "LEASH" in t["help"]
    finally:
        brain.shutdown()


def test_calibration_moves_the_um_bounds():
    _cfg, brain = _brain()
    try:
        before = _by_id(build_manifest(brain))["position_y"]["max"]
        brain.set_calibration(1, 0.1)
        time.sleep(0.1)
        after = _by_id(build_manifest(brain))["position_y"]["max"]
        assert after == pytest.approx(2 * before)
    finally:
        brain.shutdown()


def test_position_settle_is_adopt_then_flag_on_the_commanded_um():
    _cfg, brain = _brain()
    try:
        p = _by_id(build_manifest(brain))["position_x"]
        assert p["unit"] == "um" and p["set"]["verb"] == "move_to_um"
        assert p["settle"] == {"policy": "adopt_then_flag", "setpoint_key": "target_um",
                               "flag_key": "moving", "invert": True, "index": 0}
        assert p["stream"] == {"group": "position", "channel": "x"}
        brain.move_to_um(0, 12.345)            # kept verbatim, so adopt can match
        time.sleep(0.1)
        assert brain.status().target_um[0] == 12.345
    finally:
        brain.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    brain, _ = build_sim_system(Config())
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    client = AgilisClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    try:
        client.start()
        m = client.describe()
        assert m["module"] == "agilis"
        direct = client._rpc(cmd="status")["status"]
        assert direct["describe_rev"] == m["revision"]
    finally:
        client.close()
        svc.stop()


# --------------------------------------------------------------------------- #
# Declared TYPES (2026-10-04): scan-core STORES every recorded value in the
# type its descriptor declares (INSTRUMENT_MODULE_GUIDE.md 6b, developer
# notes 4b). An int outside an indicator's min/max STOPS a scan; an enum
# value that is not one of the options is lost as "not measured".
# --------------------------------------------------------------------------- #
def _type_problem(d, v):
    """Why status value `v` does not fit descriptor `d`, or "" (the same rule
    as tools/check_modules.py and scan_core/storage.py). None always fits."""
    items = v if isinstance(v, list) else [v]
    for x in items:
        if x is None:
            continue
        t = d["type"]
        if t == "bool" and not isinstance(x, bool):
            return f"declared bool, reads {x!r}"
        if t == "int":
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x != int(x):
                return f"declared int, reads {x!r}"
            if d["kind"] == "indicator":
                lo, hi = d.get("min"), d.get("max")
                if d.get("bits") is not None:
                    lo, hi = 0, 2 ** int(d["bits"]) - 1
                if (lo is not None and x < lo) or (hi is not None and x > hi):
                    return f"reads {x!r}, outside [{lo}, {hi}]"
        if t == "enum" and x not in (d.get("options") or []):
            return f"reads {x!r}, not one of {d.get('options')}"
        if t == "string" and not isinstance(x, str):
            return f"declared string, reads {x!r}"
        if t == "float" and (isinstance(x, (bool, str)) or not isinstance(x, (int, float))):
            return f"declared float, reads {x!r}"
    return ""


def _check_types(manifest, status):
    """Every readable descriptor's status value fits its declared type."""
    bad = []
    for d in manifest["parameters"]:
        if d["kind"] in ("indicator", "control") and d.get("read_path"):
            why = _type_problem(d, read_path(status, d["read_path"]))
            if why:
                bad.append(f"{d['id']}: {why}")
    assert not bad, bad


def test_declared_types_fit_status():
    from agilis.net.protocol import status_to_dict
    _cfg, brain = _brain()
    try:
        _check_types(build_manifest(brain), status_to_dict(brain.status()))
        brain.move_to_step(0, 200)
        time.sleep(0.05)
        _check_types(build_manifest(brain), status_to_dict(brain.status()))
    finally:
        brain.shutdown()


def test_step_counter_is_an_unbounded_int():
    """The AG-UC2's own counter (datum arbitrary, stage movable by hand):
    a min/max would be a promise the hardware does not keep."""
    _cfg, brain = _brain()
    try:
        params = _by_id(build_manifest(brain))
        for ax in "xy":
            d = params[f"steps_{ax}"]
            assert d["type"] == "int"
            assert "min" not in d and "max" not in d and "bits" not in d
    finally:
        brain.shutdown()
