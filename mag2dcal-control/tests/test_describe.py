"""describe: ids, verbs and settle blocks are a CONTRACT.

Two contracts, in fact. The first is with scan-core and vna-control, which read
these ids to build a registry and to follow a field. The second is with
mag2d-control: this module is a drop-in alternative to it, so every id mag2d
publishes must exist here with the same meaning. Rename one and a saved recipe
stops working against one of the two magnets.
"""

import pytest

from mag2dcal.backends.sim import FakeClock
from mag2dcal.calibration import AxisCalibration, Calibration
from mag2dcal.config import Config
from mag2dcal.net.describe import build_manifest, read_path
from mag2dcal.net.protocol import status_to_dict
from mag2dcal.sim_system import build_sim_system

#: Every descriptor id of mag2d-control. Ours is a superset.
MAG2D_IDS = {
    "field", "angle", "bx", "by", "output", "water_bypass",
    "state", "field_stable", "measured_bx", "measured_by", "measured_magnitude",
    "measured_angle", "error", "output_x", "output_y", "hall_x", "hall_y",
    "temp1", "temp2", "water_ok", "fault", "zero", "clear_fault",
}


def _ctrl(tmp_path=None):
    cfg = Config()
    cfg.calibration.load_newest_on_start = False
    clock = FakeClock()
    ctrl, _ = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=1)
    return ctrl, clock


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def _toy(gain=20.0, v=5.0, n=5):
    up = [(-v + 2 * v * i / (n - 1), gain * (-v + 2 * v * i / (n - 1)) + 0.4)
          for i in range(n)]
    return AxisCalibration(up=up, down=[(V, B - 0.8) for V, B in up])


def test_module_and_contract_ids():
    ctrl, _ = _ctrl()
    m = build_manifest(ctrl)
    assert m["module"] == "mag2dcal"
    p = _by_id(m)
    assert MAG2D_IDS <= set(p), f"missing mag2d ids: {MAG2D_IDS - set(p)}"
    for pid in ("field", "angle", "bx", "by", "output", "water_bypass", "stabilizer"):
        assert p[pid]["kind"] == "control", pid
    for pid in ("measured_bx", "measured_by", "measured_magnitude", "measured_angle",
                "error", "field_stable", "state", "temp1", "temp2", "water_ok",
                "output_x", "output_y", "frozen", "calibrated",
                "calibration_progress"):
        assert p[pid]["kind"] == "indicator", pid
    for pid in ("zero", "clear_fault", "calibrate"):
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
    assert p["stabilizer"]["set"] == {"verb": "set_stabilizer", "arg": "enabled"}
    assert p["stabilizer"]["settle"] == {"policy": "echoes", "key": "stabilizer"}


def test_the_state_indicator_lists_every_state():
    from mag2dcal.controller import STATES
    ctrl, _ = _ctrl()
    options = _by_id(build_manifest(ctrl))["state"]["options"]
    assert set(options) == set(STATES)


def test_calibrate_declares_its_arguments_and_is_dangerous():
    ctrl, _ = _ctrl()
    d = _by_id(build_manifest(ctrl))["calibrate"]
    assert d["danger"] is True
    names = {a["name"]: a for a in d["args"]}
    assert set(names) == {"n_per_leg", "dwell_s", "v_max"}
    assert names["n_per_leg"]["default"] == ctrl.cfg.calibration.n_per_leg
    assert names["v_max"]["max"] == abs(ctrl.cfg.limits.ao_limit_V)


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


def test_bounds_follow_the_calibration_too():
    """The bit that is new here: a calibration IS a limit. Loading one must move
    the sliders and the revision, or a client keeps drawing the old range."""
    ctrl, _ = _ctrl()
    m1 = build_manifest(ctrl)
    assert "NO CALIBRATION" in _by_id(m1)["field"]["help"]

    ctrl.set_calibration(Calibration(axes=[_toy(gain=20.0), _toy(gain=20.0)]))
    m2 = build_manifest(ctrl)
    p = _by_id(m2)
    assert p["field"]["max"] == pytest.approx(99.6)      # 5 V * 20 mT/V - h
    assert p["bx"]["min"] == pytest.approx(-99.6)
    assert "NO CALIBRATION" not in p["field"]["help"]
    assert m2["revision"] != m1["revision"]

    ctrl.set_calibration(None)
    assert build_manifest(ctrl)["revision"] == m1["revision"]


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
