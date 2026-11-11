"""Calibrate stage + the stabiliser's wrong-way guard (Blue Box, 2026-11-11).

On the Blue Box xy_map was inverted on both axes ("+X-Y" where "-X+Y" was
right): the stabiliser walked the sample AWAY from the laser, and nothing
said why. Now the camera can MEASURE xy_map (move each stage axis both ways,
watch the pattern), and the stabiliser stops itself when two corrections in a
row make the distance larger.

The simulator's stage is aligned with the image (+stage x -> +image x), so the
true map there is "+X+Y".
"""

from __future__ import annotations

import time

import pytest

from camera.config import Config, xy_map_from_moves
from camera.sim_system import build_sim_system


def _wait(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def tracked():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.stabilizer.settle_s = 0.05
    brain, cam, xy, z = build_sim_system(cfg)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    brain.events = events
    yield brain
    brain.shutdown()


def test_the_map_from_measured_shifts():
    # Blue Box numbers: +X moved the image LEFT, +Y moved it DOWN -> "-X+Y"
    assert xy_map_from_moves((-28.3, -2.0), (-0.1, 34.8)) == "-X+Y"
    assert xy_map_from_moves((5.0, 0.2), (0.1, 5.0)) == "+X+Y"
    # a stage mounted at 90 deg: +X moves the image up, +Y moves it right
    assert xy_map_from_moves((0.1, -5.0), (5.0, 0.0)) == "+Y-X"


def test_calibrate_stage_finds_the_true_map_and_puts_the_stage_back(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "-X-Y"                 # wrong on purpose
    x0, y0 = brain.xy.read_xy()
    n = brain.calibrate_xy_map(3.0)
    assert _wait(lambda: brain.status().xycal_id == n and not brain.status().xycal_running,
                 timeout=30)
    st = brain.status()
    assert st.xycal_state == "OK", st.xycal_state
    res = brain.get_xycal()
    assert res["xy_map"] == "+X+Y" and st.xycal_map == "+X+Y"
    assert res["current"] == "-X-Y" and "DIFFERENT" in res["summary"]
    for k, s in res["scale"].items():                 # the sim moves what it is told
        assert s == pytest.approx(1.0, rel=0.15), (k, s)
    assert brain.cfg.hardware.xy_map == "-X-Y", "nothing is changed by the calibration"
    x1, y1 = brain.xy.read_xy()
    assert (x1, y1) == pytest.approx((x0, y0), abs=1e-6)


def test_calibrate_stage_refuses_without_a_pattern_or_with_the_stabiliser_on(tracked):
    brain = tracked
    brain.set_stabilize(True)
    with pytest.raises(RuntimeError, match="stabiliser"):
        brain.calibrate_xy_map(3.0)
    brain.set_stabilize(False)
    brain.set_tracking(False)
    assert _wait(lambda: not brain.status().tracking_on)
    with pytest.raises(RuntimeError, match="pattern"):
        brain.calibrate_xy_map(3.0)


def test_a_wrong_map_stops_the_stabiliser_instead_of_walking_away(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "-X-Y"                 # both axes inverted
    brain.cfg.stabilizer.gain = 0.5
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)   # a point off the spot
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)  # the status follows a frame later
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20), \
        "the stabiliser kept walking the sample away"
    assert any(lvl == "error" and "xy_map" in msg and "Calibrate stage" in msg
               for lvl, msg in brain.events)


def test_the_right_map_does_not_trip_the_guard(tracked):
    brain = tracked
    brain.cfg.hardware.xy_map = "+X+Y"
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=20)
    assert brain.status().stabilize_on
    assert not any("STOPPED" in msg for _lvl, msg in brain.events)


def test_stop_motion_switches_the_loops_off_and_stops_the_stages(tracked):
    brain = tracked
    calls = []
    brain.xy.stop = lambda: calls.append("xy")
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    res = brain.stop_motion()
    assert "XY stage stopped" in res["stopped"] and calls == ["xy"]
    assert _wait(lambda: not brain.status().stabilize_on)
    assert any(lvl == "warn" and msg.startswith("STOP:") for lvl, msg in brain.events)


def test_a_limit_switch_stops_the_stabiliser(tracked):
    brain = tracked
    brain.xy.limit_switches = lambda: [True, False]      # X at its limit
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20)
    assert any("limit switch" in msg for _lvl, msg in brain.events)


def test_a_stage_that_does_not_move_the_image_stops_the_stabiliser(tracked):
    brain = tracked
    brain.xy.move_xy = lambda x, y: None                 # counts, never moves
    sc = brain.cfg.scanning
    brain.set_selected_index(sc.points_x - 1, sc.points_y - 1)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().stabilize_on)
    assert _wait(lambda: not brain.status().stabilize_on, timeout=20)
    assert any("does not" in msg and "move" in msg for _lvl, msg in brain.events)
