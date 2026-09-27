"""describe: ids, verbs and settle blocks are a CONTRACT with scan-core and vna."""

from mag2d.backends.sim import FakeClock
from mag2d.config import Config
from mag2d.net.describe import build_manifest, read_path
from mag2d.net.protocol import status_to_dict
from mag2d.sim_system import build_sim_system


def _ctrl():
    cfg = Config()
    clock = FakeClock()
    ctrl, _ = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=1)
    return ctrl, clock


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_module_and_contract_ids():
    ctrl, _ = _ctrl()
    m = build_manifest(ctrl)
    assert m["module"] == "mag2d"
    p = _by_id(m)
    for pid in ("field", "angle", "bx", "by", "output", "water_bypass"):
        assert p[pid]["kind"] == "control", pid
    for pid in ("measured_bx", "measured_by", "measured_magnitude", "measured_angle",
                "error", "field_stable", "state", "temp1", "temp2", "water_ok",
                "output_x", "output_y"):
        assert p[pid]["kind"] == "indicator", pid
    for pid in ("zero", "clear_fault"):
        assert p[pid]["kind"] == "action" and "wait" not in p[pid]


def test_field_like_controls_settle_on_their_own_setpoint():
    ctrl, _ = _ctrl()
    p = _by_id(build_manifest(ctrl))
    expect = {
        "field": ("set_field", "field_mT", "setpoint_field_mT", "measured_field_mT", "mT"),
        "angle": ("set_angle", "angle_deg", "setpoint_angle_deg", "measured_angle_deg", "deg"),
        "bx": ("set_bx", "bx_mT", "setpoint_bx_mT", "measured_bx_mT", "mT"),
        "by": ("set_by", "by_mT", "setpoint_by_mT", "measured_by_mT", "mT"),
    }
    for pid, (verb, arg, sp_key, read_key, unit) in expect.items():
        d = p[pid]
        assert d["set"] == {"verb": verb, "arg": arg}, pid
        assert d["settle"] == {"policy": "adopt_then_flag", "setpoint_key": sp_key,
                               "flag_key": "field_stable"}, pid
        assert d["read_path"] == [read_key] and d["unit"] == unit and d["type"] == "float"
        assert d["timeout_s"] == ctrl.cfg.control.settle_timeout_s

    assert p["output"]["set"] == {"verb": "set_output", "arg": "enabled"}
    assert p["output"]["settle"] == {"policy": "echoes", "key": "energized"}
    assert p["water_bypass"]["danger"] is True
    assert p["water_bypass"]["set"] == {"verb": "set_water_bypass", "arg": "enabled"}


def test_bounds_follow_config_and_revision_tracks_them():
    ctrl, _ = _ctrl()
    m1 = build_manifest(ctrl)
    p = _by_id(m1)
    assert (p["field"]["min"], p["field"]["max"]) == (-180.0, 180.0)
    assert (p["angle"]["min"], p["angle"]["max"]) == (-360.0, 360.0)
    assert (p["bx"]["min"], p["by"]["max"]) == (-180.0, 180.0)

    ctrl.cfg.limits.field_max_mT = 120.0
    ctrl.cfg.control.settle_timeout_s = 12.0
    m2 = build_manifest(ctrl)
    p = _by_id(m2)
    assert (p["field"]["min"], p["field"]["max"]) == (-120.0, 120.0)
    assert p["bx"]["max"] == 120.0 and p["field"]["timeout_s"] == 12.0
    assert m2["revision"] != m1["revision"]


def test_revision_does_not_move_with_measured_values():
    ctrl, clock = _ctrl()
    ctrl.start(run_thread=False)
    r1 = build_manifest(ctrl)["revision"]
    ctrl.set_field(50.0, 20.0)
    for _ in range(100):
        clock.advance(0.02)
        ctrl.tick()
    assert build_manifest(ctrl)["revision"] == r1


def test_every_read_path_resolves_against_a_real_status():
    ctrl, clock = _ctrl()
    ctrl.start(run_thread=False)
    for _ in range(10):
        clock.advance(0.02)
        ctrl.tick()
    st = status_to_dict(ctrl.status())
    for d in build_manifest(ctrl)["parameters"]:
        if d["kind"] == "action":
            continue
        assert d["read_path"], d["id"]
        assert read_path(st, d["read_path"]) is not None, d["id"]
        if d["kind"] == "control":
            assert d.get("set"), d["id"]
    assert read_path(st, ["temp_C", 1]) == st["temp_C"][1]
