"""Brain-level tests against the closed-loop simulator (no hardware)."""

import time

import pytest

from camera import vision as V
from camera.config import Config
from camera.sim_system import build_sim_system


def _wait(cond, timeout=6.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def system():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.autofocus.drive_amplitude_v = 12.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)        # the spot position is a user step (Spot tab)
    yield brain, cam, xy, z
    brain.shutdown()


def test_spot_position_is_calibrated_not_tracked():
    """Position = the calibrated one (click-to-go, stabiliser); every frame only
    re-checks the SIZE in a box around it. Uncalibrated: no position at all."""
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        s = brain.status()
        assert s.spot_found and not s.spot_calibrated       # seen, but no position yet
        with pytest.raises(RuntimeError, match="no spot"):
            brain.click_to_go(10, 10)

        res = brain.calibrate_spot(10)
        assert abs(res["x"] - 320) < 2 and abs(res["y"] - 240) < 2
        assert res["frames"] >= 5 and res["jitter_px"] < 1.0
        assert _wait(lambda: brain.status().spot_calibrated)
        s = brain.status()
        assert (s.spot_x, s.spot_y) == (res["x"], res["y"])  # exactly the stored one

        # the per-frame check looks only in the search box around the calibration:
        # move the calibrated position away and the (unmoved) spot is no longer seen
        brain.cfg.spot.lookup_region_px = 30
        brain.cfg.spot.ref_x += 200.0
        assert _wait(lambda: not brain.status().spot_found)
        assert brain.status().spot_x == pytest.approx(res["x"] + 200.0)   # position unchanged by it
        # ...while calibrating again searches the whole frame and finds it
        res2 = brain.calibrate_spot(10)
        assert abs(res2["x"] - 320) < 2
    finally:
        brain.shutdown()


def test_focus_steps_add_up_from_the_commanded_target(system):
    brain, cam, xy, z = system
    brain.set_z(5.0)
    assert brain.step_z(+1.0) == pytest.approx(6.0)
    assert brain.step_z(+1.0) == pytest.approx(7.0)     # from the target, not a mid-walk read
    assert brain.step_z(-0.5) == pytest.approx(6.5)
    assert z.read_z() == pytest.approx(6.5)
    # a stale target gives way to the live reading (someone else moved Z)
    z.set_z(20.0)
    brain.Z_STEP_FRESH_S = 0.0
    assert brain.step_z(+1.0) == pytest.approx(21.0)
    assert brain.step_z(+1e6) == brain.cfg.limits.z_max_v  # clamped like any Z move


def test_autofocus_shows_live_frames_and_can_be_killed(system):
    """Lukáš: the view froze during autofocus. Frames (and Z) must keep coming
    during the sweep, the focus curve fills in, and Kill AF stops it."""
    brain, cam, xy, z = system
    brain.cfg.autofocus.steps = 40
    brain.cfg.autofocus.averages_per_level = 3
    brain.cfg.hardware.z_step_time_ms = 60.0
    brain.autofocus()
    assert _wait(lambda: len(brain.get_af_curve()["z"]) >= 2)
    n0 = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number >= n0 + 5)
    assert brain.status().af_running
    brain.kill_af()
    assert _wait(lambda: brain.status().af_error == "killed" and not brain.status().af_running)
    time.sleep(0.3)
    assert brain.status().af_error != "OK"       # it did not finish the sweep after all


def test_xy_jog_in_um_and_no_datum_on_the_piezo_rig(system):
    brain, cam, xy, z = system
    brain.move_xy(50.0, 60.0)
    assert _wait(lambda: not xy.moving())
    assert brain.xy_step_unit() == "um"
    assert brain.step_xy(+1.0, 0.0) == pytest.approx([51.0, 60.0])
    assert brain.step_xy(0.0, -2.5) == pytest.approx([51.0, 57.5])   # adds up from the target
    s = brain.status()
    assert not s.xy_has_datum and not s.limits_from_stage
    assert (s.x_min, s.x_max) == (brain.cfg.limits.motor_x_min, brain.cfg.limits.motor_x_max)
    with pytest.raises(RuntimeError, match="no datum"):
        brain.datum_xy()


def test_63x_objective_pixel_size_from_the_lab_calibration(system):
    """objectives.ini: at 63x the 1096-px frame height is 50 um."""
    brain, *_ = system
    assert "63x" in brain.list_objectives()
    res = brain.set_objective("63x")
    assert res["pixel_size_y_um"] == pytest.approx(50.0 / 1096, rel=1e-4)
    assert res["pixel_size_x_um"] == pytest.approx(res["pixel_size_y_um"])


def test_spot_position_entered_by_hand(system):
    brain, *_ = system
    res = brain.set_spot_position(100.5, 50.25)
    assert (res["x"], res["y"]) == (100.5, 50.25)
    assert brain.spot_position() == (100.5, 50.25)
    assert _wait(lambda: (brain.status().spot_x, brain.status().spot_y) == (100.5, 50.25))
    with pytest.raises(ValueError, match="outside"):
        brain.set_spot_position(5000, 10)                  # sim frame is 640x480
    assert brain.spot_position() == (100.5, 50.25)         # unchanged by the refusal
    brain.clear_spot_position()
    assert brain.spot_position() is None
    assert _wait(lambda: not brain.status().spot_calibrated)
    with pytest.raises(RuntimeError, match="calibrate the spot"):
        brain.click_to_go(10, 10)


def test_calibrated_spot_survives_save_and_load(tmp_path):
    from camera.config import load_config
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, *_ = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        res = brain.calibrate_spot(10)
        path = brain.save_config(str(tmp_path / "camera.ini"))
    finally:
        brain.shutdown()
    back = load_config(path)
    assert back.spot.ref_set is True
    assert back.spot.ref_x == pytest.approx(res["x"]) and back.spot.ref_y == pytest.approx(res["y"])


def _capture_and_track(brain, cam):
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)


def test_lifecycle(system):
    brain, *_ = system
    assert brain.status().connected


def test_spot_and_match(system):
    brain, cam, *_ = system
    _capture_and_track(brain, cam)
    s = brain.status()
    assert s.spot_found
    assert abs(s.spot_x - 320) < 2 and abs(s.spot_y - 240) < 2
    assert s.match_score > 0.9


def test_stabiliser_converges_to_selected_point(system):
    brain, cam, xy, z = system
    _capture_and_track(brain, cam)
    brain.set_selected_index(2, 2)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stable, timeout=6.0)
    s = brain.status()
    # the selected point now sits on the spot to within the stable tolerance
    tol_px = brain.cfg.stabilizer.stable_radius_um / brain.cfg.image.pixel_size_x_um
    assert abs(s.point_minus_spot_x) <= tol_px + 0.5
    assert abs(s.point_minus_spot_y) <= tol_px + 0.5


def test_autofocus_finds_true_focus(system):
    brain, cam, xy, z = system
    brain.set_z(4.0)
    assert _wait(lambda: abs(brain.status().z_voltage - 4.0) < 0.5)
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running, timeout=8.0)
    s = brain.status()
    assert s.af_error == "OK"
    assert abs(s.best_focus_v - z.z_focus) < 1.0   # within one sweep step


def test_spot_area_focus_metric_counts_only_the_spot(system):
    """Lukáš, 2026-09-14: the spot_area metric counted every bright pixel in the
    frame (~136 000 px of illumination vs an 885 px spot on the 63x rig)."""
    import numpy as np
    brain, cam, xy, z = system
    g = brain.latest_frame()
    spot_only = brain._focus_metric(g)
    assert 5 < spot_only < 2000
    bright = g.copy()
    bright[:150, :200] = 255                          # a saturated corner, far from the spot
    assert brain._focus_metric(bright) == pytest.approx(spot_only)
    assert V.focus_metric(bright, "spot_area") > 25_000   # the old whole-frame count
    dark = np.full_like(g, 10)
    assert np.isnan(brain._focus_metric(dark))        # no spot: NaN, never "area 0"
    brain.cfg.spot.ref_set = False
    with pytest.raises(RuntimeError, match="calibrated spot"):
        brain._focus_metric(g)


def test_move_clamps_to_limits(system):
    brain, *_ = system
    tgt = brain.move_xy(10_000, -10_000)
    assert tgt[0] == brain.cfg.limits.motor_x_max
    assert tgt[1] == brain.cfg.limits.motor_y_min
    v = brain.set_z(9999)
    assert v == brain.cfg.limits.z_max_v


def test_camera_features_list_and_set(system):
    brain, cam, *_ = system
    feats = brain.camera_features()
    names = {f["name"] for f in feats}
    assert {"ExposureTime", "Gain", "Gamma", "PixelFormat"} <= names
    # setting a parameter takes effect and is clamped to its range
    assert brain.set_camera_feature("Gain", 2.0) == 2.0
    assert brain.set_camera_feature("Gain", 999) == 16.0   # clamped to max
    # read-only + bad-enum are rejected
    with pytest.raises(Exception):
        brain.set_camera_feature("DeviceModelName", "x")
    with pytest.raises(Exception):
        brain.set_camera_feature("PixelFormat", "RGB8")


def test_camera_feature_affects_image(system):
    brain, cam, *_ = system
    m0 = cam.grab().mean()
    brain.set_camera_feature("ExposureTime", 20000)
    brain.set_camera_feature("Gain", 3.0)
    assert cam.grab().mean() > m0   # brighter after more exposure + gain


def test_objective_calibration(system):
    brain, *_ = system
    res = brain.set_objective("50x - Zeiss NA 0.8")
    assert abs(res["pixel_size_x_um"] - 0.1652) < 1e-6
    assert brain.status().pixel_size_x == pytest.approx(0.1652)


def test_capture_sets_reference_offset(system):
    brain, cam, *_ = system
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))   # array centre defaults to spot
    ref = brain.reference
    assert ref is not None
    # offset = spot - template_centre = (320-440, 240-260) = (-120, -20)
    assert abs(ref.array_center_offset_px[0] + 120) < 2
    assert abs(ref.array_center_offset_px[1] + 20) < 2


def test_set_scan_area_from_rectangle(system):
    brain, cam, *_ = system
    _capture_and_track(brain, cam)
    brain.cfg.scanning.points_x = 3
    brain.cfg.scanning.points_y = 3
    px = brain.cfg.image.pixel_size_x_um
    # a 100 px wide box over 3 points -> pitch = 100*px/2
    res = brain.set_scan_area(320, 240, 100, 80)
    assert res["dx_um"] == pytest.approx(100 * px / 2)
    assert res["dy_um"] == pytest.approx(80 * px / 2)
    assert res["repinned"] is True


def test_scan_area_angle_recall_and_size(system):
    brain, cam, *_ = system
    _capture_and_track(brain, cam)
    brain.cfg.scanning.points_x = 4
    brain.cfg.scanning.points_y = 3
    px = brain.cfg.image.pixel_size_x_um
    # draw a tilted rectangle
    res = brain.set_scan_area(320, 240, 150, 100, 25.0)
    assert res["angle_deg"] == pytest.approx(25.0)
    assert res["dx_um"] == pytest.approx(150 * px / 3)   # (points_x - 1) = 3
    # recall rebuilds the same rectangle
    rect = brain.get_scan_rect()
    assert rect is not None
    assert rect["angle"] == pytest.approx(25.0)
    assert rect["w"] == pytest.approx(150, abs=1.0)
    assert rect["h"] == pytest.approx(100, abs=1.0)
    # size -> pitch (alternative entry)
    p = brain.set_scan_size_um(30.0, 12.0)
    assert p["dx_um"] == pytest.approx(30.0 / 3)
    assert p["dy_um"] == pytest.approx(12.0 / 2)


def test_autofocus_curve_recorded(system):
    brain, *_ = system
    brain.set_z(4.0)
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running, timeout=8.0)
    c = brain.get_af_curve()
    assert len(c["z"]) == brain.cfg.autofocus.steps
    assert len(c["metric"]) == len(c["z"])
    assert c["z"][0] < c["z"][-1]


def test_accuracy_logging_accumulates(system):
    brain, cam, *_ = system
    _capture_and_track(brain, cam)
    brain.set_accuracy_logging(True)
    assert _wait(lambda: len(brain.get_accuracy()["dx"]) > 3)
    acc = brain.get_accuracy()
    assert len(acc["dx"]) == len(acc["dy"])
    brain.set_accuracy_logging(False)


def test_save_load_pattern_roundtrip(system, tmp_path):
    brain, cam, *_ = system
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.cfg.scanning.points_x = 4
    p = tmp_path / "pat.png"
    brain.save_pattern(str(p))
    brain.reference = None
    brain.load_pattern(str(p))
    assert brain.reference is not None
    assert brain.cfg.scanning.points_x == 4


def test_loaded_pattern_restores_the_whole_scanning_array(system, tmp_path):
    """Lukáš, 2026-09-14: loading a pattern must bring back ALL array parameters."""
    from dataclasses import asdict
    brain, cam, *_ = system
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    sc = brain.cfg.scanning
    sc.points_x, sc.points_y, sc.dx_um, sc.dy_um = 20, 7, 3.5, 1.25
    sc.angle_deg, sc.selected_index_x, sc.selected_index_y = 36.4982, 4, 6
    sc.overlay_size, sc.overlay_style = 3, "open"
    brain.reference.array_center_offset_px = (-40.5, 12.25)
    want = asdict(sc)
    p = tmp_path / "pat.png"
    brain.save_pattern(str(p))

    for k, v in (("points_x", 3), ("points_y", 3), ("dx_um", 1.0), ("dy_um", 1.0),
                 ("angle_deg", 0.0), ("selected_index_x", 0), ("selected_index_y", 0),
                 ("overlay_size", 5), ("overlay_style", "fill")):
        setattr(sc, k, v)
    brain.load_pattern(str(p))
    assert asdict(brain.cfg.scanning) == pytest.approx(want)
    assert brain.reference.array_center_offset_px == (-40.5, 12.25)
