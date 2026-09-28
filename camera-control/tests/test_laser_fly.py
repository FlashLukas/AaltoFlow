"""The laser ON THE SAMPLE, for fly scans in camera coordinates.

`laser_x` / `laser_y` = where the laser is, in um from the main template
(spot_from_template), measured optically every frame -- immune to an
open-loop stage's counter drift. Setting them PLACES the laser there (the
stabiliser's recipe aimed at a free point); streaming them lets scan-core's
fly scan bin a detector by the camera's coordinates.
"""

import math
import time

import pytest

from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system


def _wait(cond, timeout=8.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def tracked():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: math.isfinite(brain.status().spot_from_template_x_um))
    yield brain, xy
    brain.shutdown()


def _here(brain):
    s = brain.status()
    return s.spot_from_template_x_um, s.spot_from_template_y_um


def test_setting_a_target_places_the_laser_there_and_lets_go(tracked):
    brain, xy = tracked
    x0, y0 = _here(brain)
    brain.set_laser_target(x0 + 6.0, y0 - 4.0)
    assert _wait(lambda: brain.status().laser_settled, timeout=10.0)
    x, y = _here(brain)
    r = brain.cfg.stabilizer.stable_radius_um
    assert math.hypot(x - (x0 + 6.0), y - (y0 - 4.0)) <= r + 0.05
    s = brain.status()
    assert not s.laser_goto                     # the loop let go of the stage
    assert (s.laser_target_x_um, s.laser_target_y_um) == pytest.approx((x0 + 6, y0 - 4))


def test_settled_is_re_checked_every_frame(tracked):
    """After a fly row has taken the stage away, a frame must not still say
    "settled" at the old target -- the next row's approach would return at
    once and never move."""
    brain, xy = tracked
    brain.set_laser_target(*_here(brain))
    assert _wait(lambda: brain.status().laser_settled, timeout=10.0)
    x, y = xy.read_xy()
    xy.move_xy(x + 8.0, y)                      # something else moves the sample
    assert _wait(lambda: not brain.status().laser_settled, timeout=3.0)


def test_one_coordinate_at_a_time_keeps_the_other(tracked):
    brain, xy = tracked
    x0, y0 = _here(brain)
    assert brain.set_laser_target(y=y0 + 3.0) == pytest.approx([x0, y0 + 3.0], abs=0.3)
    assert brain.set_laser_target(x=x0 - 2.0) == pytest.approx([x0 - 2.0, y0 + 3.0], abs=0.3)


def test_placing_needs_a_template_and_a_spot():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        with pytest.raises(RuntimeError, match="no template"):
            brain.set_laser_target(1.0, 1.0)
    finally:
        brain.shutdown()


def test_the_stream_records_the_laser_while_the_sample_moves(tracked):
    brain, xy = tracked
    brain.stream.start()
    x, y = xy.read_xy()
    for k in range(10):
        xy.move_xy(x + 0.5 * k, y)
        time.sleep(0.03)
    c = brain.stream.stop()
    lx = [v for v in c["values"]["laser_x"] if v is not None]
    assert len(c["t"]) >= 10 and len(lx) >= 10
    assert abs(lx[-1] - lx[0]) > 2.0            # it saw the sample move
    assert c["delay_s"] == {"laser_x": 0.0, "laser_y": 0.0}


def test_the_stabiliser_stands_down_while_a_fly_scan_records(tracked):
    """A fly scan moves the sample ON PURPOSE; a stabiliser that pulled it back
    would fight the stage for the whole row."""
    brain, xy = tracked
    brain.cfg.scanning.points_x = brain.cfg.scanning.points_y = 1
    brain.set_selected_index(0, 0)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=10.0)
    brain.stream.start()
    try:
        x, y = xy.read_xy()
        xy.move_xy(x + 10.0, y)
        time.sleep(0.5)
        assert xy.read_xy()[0] == pytest.approx(x + 10.0, abs=0.2)   # not pulled back
        assert brain.status().streaming and not brain.status().point_settled
    finally:
        brain.stream.stop()
    # and it comes back afterwards
    assert _wait(lambda: brain.status().point_settled, timeout=10.0)


def test_describe_offers_the_laser_coordinates_with_their_stream():
    cfg = Config()
    brain, *_ = build_sim_system(cfg)
    params = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    for ax in ("x", "y"):
        p = params[f"laser_{ax}"]
        assert p["kind"] == "control" and p["unit"] == "um"
        assert p["stream"] == {"group": "laser", "channel": f"laser_{ax}"}
        assert p["settle"]["flag_key"] == "laser_settled"
        assert p["min"] < 0 < p["max"]


def test_placing_the_laser_switches_the_array_stabiliser_off_and_back(tracked):
    """Two targets for one stage would fight: after a placement let go, the
    stabiliser would drag the laser back to its array point."""
    brain, xy = tracked
    brain.set_stabilize(True)
    brain.set_laser_target(*_here(brain))
    assert not brain.status().stabilize_on or _wait(lambda: not brain.status().stabilize_on)
    brain.set_stabilize(True)                   # and the other way round
    assert not brain._laser_goto


def test_a_target_arriving_mid_frame_is_not_marked_done():
    """The engine measures a frame against the OLD target; a new target arrives
    (request thread) before that frame reaches its decision. The old frame
    must not fill the new window or mark the new target done -- otherwise the
    loop stops without moving and laser_settled never comes."""
    from camera.camera import CameraStatus

    cfg = Config()
    cfg.stabilizer.images_to_average = 1       # one frame decides
    brain, _cam, _xy, _z = build_sim_system(cfg)   # not started: we drive the step
    brain._commit_laser_target((0.0, 0.0))     # the laser IS at this one
    new = (5.0, 0.0)
    # _stage_settling runs after the step has read the target and before it
    # decides: the moment a request would slip in.
    brain._stage_settling = lambda: (brain._commit_laser_target(new), False)[1]
    st = CameraStatus(spot_x=100.0, spot_y=100.0)
    brain._laser_step((100.0, 100.0), 0.1, 0.1, st)
    assert brain._laser_target == new
    assert brain._laser_goto and not brain._laser_done
    assert len(brain._avg_buf) == 0
