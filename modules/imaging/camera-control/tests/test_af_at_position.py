"""Autofocus AT THE AF POSITION, then back (2026-10-10).

Sometimes focus must be found somewhere else than where we measure -- a
feature with contrast, a clean area. `autofocus_at_position` remembers where
the laser is held, takes it to the AF position (an array point, or um from the
main template), finds focus there, brings it back and waits until it is there
again. Z stays at the new focus. It is ONE numbered run (af_id), so a scan
waits for the whole round trip with the autofocus's own wait block.

Proved here, on the camera simulator (a flat sample: focus is the same Z
everywhere, so an autofocus anywhere finds the measuring point's focus):
  * the round trip ends on the original point (array point, or laser target),
    and Z moved to focus;
  * the AF position as an array INDEX and as um from the template;
  * a failing autofocus still brings the laser back, then reports af_error;
  * Kill AF stops the trip where it is (no loop moves the stage any more);
  * per-run settings override the camera's and are put back afterwards;
  * refused (nothing moves) without tracking / a calibrated spot / a position;
  * an AF position far enough away that the MAIN template leaves the image
    works through a backup pattern (the anchor logic);
  * describe offers it with the autofocus's wait block and its arguments.
"""

import math
import time

import numpy as np
import pytest

from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system


def _wait(cond, timeout=20.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _cfg():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.autofocus.drive_amplitude_v = 12.0
    cfg.autofocus.af_trip_settle_s = 30.0
    # a 5 x 5 array, 3 um pitch: the far corner is ~8.5 um from the centre
    cfg.scanning.points_x = cfg.scanning.points_y = 5
    cfg.scanning.dx_um = cfg.scanning.dy_um = 3.0
    return cfg


def _tracked(cfg=None):
    brain, cam, xy, z = build_sim_system(cfg or _cfg())
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: math.isfinite(brain.status().spot_from_template_x_um))
    return brain, cam, xy, z


@pytest.fixture
def tracked():
    brain, cam, xy, z = _tracked()
    yield brain, cam, xy, z
    brain.shutdown()


def _here(brain):
    s = brain.status()
    return s.spot_from_template_x_um, s.spot_from_template_y_um


def _done(brain, n, timeout=60.0):
    assert _wait(lambda: brain.status().af_id == n and not brain.status().af_running,
                 timeout=timeout), f"run {n} never finished: {brain.status().af_error}"
    return brain.status()


def _held_at_centre(brain):
    """Stabiliser holding the array's centre point (2, 2)."""
    brain.set_selected_index(2, 2)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=20.0)


def test_round_trip_to_an_array_point_comes_back_and_moves_z(tracked):
    brain, cam, xy, z = tracked
    _held_at_centre(brain)
    x0, y0 = _here(brain)
    z.set_z(4.0)                      # out of focus (the scene's focus is at 7.6)
    assert _wait(lambda: abs(z.read_z() - 4.0) < 1e-6)
    z0 = z.read_z()
    visited = []
    n = brain.autofocus_at_position(ix=0, iy=0)
    s = brain.status()
    assert s.af_id == n and s.af_running and s.af_trip == "to_af"
    # watch the trip: it must really get to the far corner on the way
    t_end = time.monotonic() + 60
    while time.monotonic() < t_end:
        s = brain.status()
        if s.af_trip == "focus":
            visited.append(_here(brain))
        if s.af_id == n and not s.af_running:
            break
        time.sleep(0.02)
    s = _done(brain, n)
    assert s.af_error == "OK", s.af_error
    assert visited, "the autofocus never ran"
    # at the corner (0, 0) of a 3 um grid centred on (2, 2): 6 um away in x and y
    vx, vy = visited[0]
    assert abs((vx - x0) + 6.0) < 1.0 and abs((vy - y0) + 6.0) < 1.0, (vx, vy, x0, y0)
    # back on the measuring point, stabiliser holding it again, index restored
    assert s.point_settled and s.stabilize_on
    assert (s.selected_index_x, s.selected_index_y) == (2, 2)
    x1, y1 = _here(brain)
    r = brain.cfg.stabilizer.stable_radius_um
    assert math.hypot(x1 - x0, y1 - y0) <= r + 0.1
    # Z moved to the focus the autofocus found, and stayed there
    assert abs(z.read_z() - z0) > 2.0
    assert z.read_z() == pytest.approx(7.6, abs=0.5)


def test_round_trip_to_a_point_in_um_returns_to_the_laser_target(tracked):
    brain, cam, xy, z = tracked
    x0, y0 = _here(brain)
    brain.set_laser_target(x0 + 2.0, y0 + 1.0)       # the measuring point
    assert _wait(lambda: brain.status().laser_settled, timeout=20.0)
    n = brain.autofocus_at_position(x_um=x0 - 5.0, y_um=y0 + 4.0)
    s = _done(brain, n)
    assert s.af_error == "OK", s.af_error
    assert not s.stabilize_on                          # it was off: still off
    assert (s.laser_target_x_um, s.laser_target_y_um) == pytest.approx((x0 + 2, y0 + 1))
    assert s.laser_settled
    x1, y1 = _here(brain)
    assert math.hypot(x1 - (x0 + 2.0), y1 - (y0 + 1.0)) <= \
        brain.cfg.stabilizer.stable_radius_um + 0.1


def test_the_configured_af_position_is_used_when_no_args(tracked):
    brain, cam, xy, z = tracked
    with pytest.raises(RuntimeError, match="no AF position"):
        brain.autofocus_at_position()
    _held_at_centre(brain)
    pos = brain.set_af_position("index", 4, 2)
    assert pos == {"set": True, "position": "index", "ix": 4, "iy": 2,
                   "x_um": 0.0, "y_um": 0.0}
    seen = []
    orig = brain._trip_go
    brain._trip_go = lambda kind, value, what: (seen.append((kind, tuple(value))),
                                                orig(kind, value, what))[1]
    n = brain.autofocus_at_position()
    s = _done(brain, n)
    assert s.af_error == "OK", s.af_error
    assert seen == [("index", (4, 2)), ("index", (2, 2))]


def test_set_af_position_here_takes_the_laser_position(tracked):
    brain, *_ = tracked
    x0, y0 = _here(brain)
    pos = brain.set_af_position_here()
    assert pos["set"] and pos["position"] == "um"
    assert (pos["x_um"], pos["y_um"]) == pytest.approx((x0, y0), abs=0.3)
    _held_at_centre(brain)
    pos = brain.set_af_position_here()               # stabiliser on -> the array point
    assert (pos["position"], pos["ix"], pos["iy"]) == ("index", 2, 2)
    brain.clear_af_position()
    assert not brain.af_position()["set"]


def test_a_failing_autofocus_still_comes_back_then_reports_the_error(tracked):
    brain, cam, xy, z = tracked
    _held_at_centre(brain)
    x0, y0 = _here(brain)

    def broken(go, live):
        raise RuntimeError("no focus here (test)")
    brain._af_one_way = broken
    n = brain.autofocus_at_position(ix=4, iy=4, routine="one_way")
    s = _done(brain, n)
    assert "no focus here" in s.af_error and s.af_error != "OK"
    # ... but the laser is back on the measuring point before that was said
    assert s.point_settled and (s.selected_index_x, s.selected_index_y) == (2, 2)
    x1, y1 = _here(brain)
    assert math.hypot(x1 - x0, y1 - y0) <= brain.cfg.stabilizer.stable_radius_um + 0.1


def test_kill_af_stops_the_trip_where_it_is(tracked):
    brain, cam, xy, z = tracked
    _held_at_centre(brain)
    brain.cfg.stabilizer.gain = 0.2          # slow, so the kill lands mid-way
    n = brain.autofocus_at_position(ix=0, iy=0)
    assert _wait(lambda: brain.status().af_trip == "to_af")
    time.sleep(0.3)
    brain.kill_af()
    s = _done(brain, n, timeout=15.0)
    assert s.af_error == "killed"
    assert not s.stabilize_on and not s.laser_goto   # nothing pulls the stage any more
    # the selected point is the measuring point again (switching the
    # stabiliser on would bring the laser back there)
    assert (s.selected_index_x, s.selected_index_y) == (2, 2)
    p1 = xy.read_xy()
    time.sleep(0.5)
    assert np.allclose(xy.read_xy(), p1, atol=1e-6)  # the stage stays where it is


def test_kill_af_during_the_autofocus_stops_everything(tracked):
    brain, cam, xy, z = tracked
    _held_at_centre(brain)
    brain.cfg.autofocus.steps = 61                    # a long sweep to kill
    n = brain.autofocus_at_position(ix=1, iy=2)
    assert _wait(lambda: brain.status().af_trip == "focus"
                 and brain.status().af_error == "running", timeout=30.0)
    brain.kill_af()
    s = _done(brain, n, timeout=15.0)
    assert s.af_error == "killed" and not s.stabilize_on
    assert brain._trip_phase is None


def test_settings_override_the_camera_for_one_run_and_are_put_back(tracked):
    brain, cam, xy, z = tracked
    _held_at_centre(brain)
    af = brain.cfg.autofocus
    before = (af.routine, af.steps, af.drive_amplitude_v, af.mechanism)
    seen = {}
    orig = brain._run_autofocus

    def spy(req):
        seen.update(routine=af.routine, steps=af.steps,
                    drive=af.drive_amplitude_v, mech=af.mechanism)
        orig(req)
    brain._run_autofocus = spy
    n = brain.autofocus_at_position(ix=2, iy=1, steps=7, drive_amplitude_v=10.0,
                                    mechanism="spot_d4sigma")
    s = _done(brain, n)
    assert s.af_error == "OK", s.af_error
    assert seen == {"routine": "sweep", "steps": 7, "drive": 10.0, "mech": "spot_d4sigma"}
    assert len(brain.get_af_curve()["z"]) == 7        # the sweep really had 7 levels
    assert (af.routine, af.steps, af.drive_amplitude_v, af.mechanism) == before


def test_bad_arguments_are_refused_before_anything_moves(tracked):
    brain, cam, xy, z = tracked
    p0 = xy.read_xy()
    for kw, msg in (({"ix": 9, "iy": 0}, "outside the array"),
                    ({"ix": 1, "x_um": 2.0}, "not both"),
                    ({"ix": 1, "iy": 1, "routine": "zigzag"}, "routine must be"),
                    ({"ix": 1, "iy": 1, "steps": 0}, "whole number"),
                    ({"ix": 1, "iy": 1, "colour": "red"}, "no setting called"),
                    ({"position": "polar"}, "position must be")):
        with pytest.raises((ValueError, RuntimeError), match=msg):
            brain.autofocus_at_position(**kw)
    assert brain.status().af_id == 0 and not brain.status().af_running
    assert np.allclose(xy.read_xy(), p0)


def test_refused_without_tracking_or_a_calibrated_spot():
    brain, cam, xy, z = build_sim_system(_cfg())
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        with pytest.raises(RuntimeError, match="no template tracked"):
            brain.autofocus_at_position(ix=0, iy=0)
        tcx, tcy = cam.template_center_px()
        brain.capture_reference((tcx, tcy, 60, 60))
        brain.set_tracking(True)
        with pytest.raises(RuntimeError, match="calibrate the spot"):
            brain.autofocus_at_position(ix=0, iy=0)
    finally:
        brain.shutdown()


def test_a_plain_autofocus_is_refused_during_a_trip(tracked):
    brain, *_ = tracked
    _held_at_centre(brain)
    n = brain.autofocus_at_position(ix=0, iy=2)
    with pytest.raises(RuntimeError, match="AF position is running"):
        brain.autofocus()
    with pytest.raises(RuntimeError, match="already running"):
        brain.autofocus_at_position(ix=0, iy=2)
    _done(brain, n)


def test_the_af_position_travels_with_the_pattern(tracked, tmp_path):
    brain, *_ = tracked
    brain.set_af_position("um", x_um=4.5, y_um=-3.0)
    path = str(tmp_path / "p.png")
    brain.save_pattern(path)
    brain.clear_af_position()
    brain.cfg.scanning.af_x_um = 0.0
    brain.load_pattern(path)
    assert brain.af_position() == {"set": True, "position": "um", "ix": 0, "iy": 0,
                                   "x_um": 4.5, "y_um": -3.0}


def test_describe_offers_the_round_trip_with_the_autofocus_wait_and_args():
    brain, *_ = build_sim_system(_cfg())
    params = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    a, plain = params["autofocus_at_position"], params["autofocus"]
    assert a["kind"] == "action"
    assert a["wait"]["target_key"] == plain["wait"]["target_key"] == "af_id"
    assert a["wait"]["ready"] == plain["wait"]["ready"]
    assert a["wait"]["check"] == {"key": "af_error", "equals": "OK"}
    args = {x["name"]: x for x in a["args"]}
    assert set(args) >= {"position", "ix", "iy", "x_um", "y_um", "go_back", "routine",
                         "mechanism", "exposure_us", "drive_amplitude_v", "steps"}
    assert args["ix"]["max"] == 4 and args["routine"]["options"] == ["sweep", "one_way"]
    # only go_back has a default: everything else left out = the camera's own
    assert [n for n, x in args.items() if "default" in x] == ["go_back"]
    assert params["af_trip"]["kind"] == "indicator"


# --------------------------------------------------------------------------- #
# backup patterns: an AF position where the main template is off-screen
# --------------------------------------------------------------------------- #
class TwoPatternCamera:
    """The simulator's scene with a SECOND, different feature on the sample
    (the backup pattern), BACKUP_OFFSET px from the main template."""

    BACKUP_OFFSET = (-380.0, 120.0)

    @staticmethod
    def make(stage, zfocus, **kw):
        from camera.backends.sim import SimCamera

        class _Cam(SimCamera):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                # rotated + mirrored: matches itself, not the main glyph
                self._glyph2 = np.ascontiguousarray(np.rot90(self._glyph)[:, ::-1])

            def _paste_subpixel(self, frame, stamp, cx, cy):
                super()._paste_subpixel(frame, stamp, cx, cy)
                if stamp is self._glyph:
                    ox, oy = TwoPatternCamera.BACKUP_OFFSET
                    super()._paste_subpixel(frame, self._glyph2, cx + ox, cy + oy)
        return _Cam(stage, zfocus, **kw)


def test_an_af_position_off_screen_of_the_main_template_works_via_a_backup():
    """The scan array (and a point in um) hang off the MAIN template's position,
    which a backup pattern keeps known while the main one is out of the image.
    The AF position is placed so far that the main template leaves the frame
    on the way; the trip must still get there and back."""
    from camera.backends.sim import SimXYStage, SimZFocus
    from camera.camera import Camera

    cfg = _cfg()
    cfg.stabilizer.gain = 0.3           # smaller moves: the pattern stays in its search box
    cfg.stabilizer.settle_s = 0.05
    cfg.pattern.safety_area_px = 200
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimZFocus(z0=cfg.hardware.z_step_v * 30, z_focus=7.6,
                  vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    # the main template starts near the RIGHT edge (540 of 640 px), the backup
    # 380 px to its left: a 150 px shift pushes the main one out of the frame
    px = cfg.image.pixel_size_x_um
    cam = TwoPatternCamera.make(xy, z, pixel_size_x_um=px, pixel_size_y_um=px,
                                template_home_px=(540.0, 260.0))
    brain = Camera(cam, xy, z, cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        brain.calibrate_spot(10)
        tcx, tcy = cam.template_center_px()
        brain.capture_reference((tcx, tcy, 60, 60))
        brain.set_tracking(True)
        assert _wait(lambda: brain.status().match_found)
        ox, oy = TwoPatternCamera.BACKUP_OFFSET
        brain.capture_backup((tcx + ox, tcy + oy, 60, 60))
        assert _wait(lambda: brain.status().backups_n == 1)
        x0, y0 = _here(brain)
        # the point 150 px left of the laser goes under it: the sample shifts
        # 150 px right, the main template (at 540 px) ends at ~690 px --
        # outside the 640 px frame -- while the backup slides towards the middle
        px = brain.cfg.image.pixel_size_x_um
        n = brain.autofocus_at_position(x_um=x0 - 150.0 * px, y_um=y0)
        drivers, anchors = set(), []
        t_end = time.monotonic() + 120
        while time.monotonic() < t_end:
            s = brain.status()
            drivers.add(s.pattern_driver)
            anchors.append(s.anchor_x)
            if s.af_id == n and not s.af_running:
                break
            time.sleep(0.02)
        assert s.af_id == n and not s.af_running
        assert s.af_error == "OK", s.af_error
        assert max(anchors) > 640, "the main template never left the image"
        assert drivers - {0}, "a backup pattern never drove: the test did not test it"
        x1, y1 = _here(brain)
        assert math.hypot(x1 - x0, y1 - y0) <= brain.cfg.stabilizer.stable_radius_um + 0.15
    finally:
        brain.shutdown()


def test_the_round_trip_over_the_wire_with_arguments():
    """The verb takes its arguments by name; a typo is an error reply, and the
    reply carries the run number a scan waits on."""
    from camera.net.client import CameraClient
    from camera.net.service import CameraService

    brain, cam, xy, z = build_sim_system(_cfg())
    svc = CameraService(brain, host="127.0.0.1", cmd_port=15771, pub_port=15772,
                        status_hz=20)
    svc.start()
    cli = CameraClient("127.0.0.1", 15771, 15772, timeout_ms=3000)
    cli.start()
    try:
        assert _wait(lambda: cli.status().frame_number > 2)
        cli.calibrate_spot(10)
        tcx, tcy = cam.template_center_px()
        cli.capture_reference((tcx, tcy, 60, 60))
        cli.set_tracking(True)
        assert _wait(lambda: brain.status().match_found)
        assert cli.set_af_position(ix=1, iy=3)["position"] == "index"
        assert cli.af_position()["iy"] == 3
        with pytest.raises(Exception, match="no setting called"):
            cli.autofocus_at_position(ix=1, iy=1, colour="red")
        n = cli.autofocus_at_position(steps=5, go_back=True)
        assert _wait(lambda: cli.status().af_id == n and not cli.status().af_running,
                     timeout=60.0)
        assert cli.status().af_error == "OK"
        assert len(brain.get_af_curve()["z"]) == 5
    finally:
        cli.close()
        svc.stop()
