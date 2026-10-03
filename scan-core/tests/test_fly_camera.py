"""Flying in the CAMERA's coordinates: `move` on a fly axis.

The camera measures where the laser is on the SAMPLE (camera.laser_x, um from
the template); the KIM stage only knows its step counter, which drifts. Here
the axis is the camera coordinate and the stage named by `move` flies it:

    {type: fly, param: cam.lx, start, stop, num, move: stage.x, speed, ...}

The fake world below is deliberately unkind, as a real rig can be:
  * the camera coordinate runs the OTHER way from the stage and 1.2x as fast
    (a mount rotated 180 deg and a step size that is not what the counter says);
  * the stage-to-sample offset DRIFTS over time (the counter drift seen on the
    rig).
The binned signal must still land at the right camera coordinates, because
the grid, the placement of each row and the binning never use the counter.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.registry import Gettable, Registry, Settable, StreamSpec
from timed_stream import TimedStream, Track


def timed(group, sample_at, rate_hz):
    """A stream sampled on its own clock (timed_stream.py): the sample counts
    per pixel then depend on where the stage went, not on how promptly this
    test's threads were scheduled."""
    st = TimedStream(sample_at, rate_hz)
    return StreamSpec(group, st.start, st.read, st.stop)


class World:
    """One stage (moved at a speed) and a camera that sees the sample.

    The stage is a Track, which remembers every move, so the streams can ask
    where the laser was at any past moment (see timed_stream.py for why)."""

    def __init__(self, gain=-1.2, offset=7.0, drift_um_per_s=0.4):
        self.lock = threading.Lock()
        self.track = Track()
        self.speed = 50.0
        self.gain, self.offset, self.drift = gain, offset, drift_um_per_s
        self.t_start = time.time()
        self.gen = 0

    def stage(self, t=None):
        return self.track.pos(t)

    def cam(self, t=None):
        """Where the laser is on the sample, as the camera measures it."""
        t = time.time() if t is None else t
        return self.offset + self.drift * (t - self.t_start) + self.gain * self.stage(t)

    def move(self, target, wait=True):
        with self.lock:
            t1 = self.track.move(target, self.speed)
            self.gen += 1
            me = self.gen
        while wait and time.time() < t1 and self.gen == me:
            time.sleep(0.002)


def signal(x):
    """What the detector sees at SAMPLE position x: a bump at +2 um."""
    return np.exp(-((x - 2.0) / 3.0) ** 2)


def build(world):
    reg = Registry()

    def stage_set(v, timeout_s=None):
        world.move(v)

    reg.add(Settable("stage.x", "Stage X", "um", (-500, 500), stage_set, world.stage))
    reg.add(Settable("stage.speed", "Stage speed", "um/s", (0.1, 500),
                     lambda v: setattr(world, "speed", v), lambda: world.speed))

    def place(target):
        """The camera's closed-loop placement: iterate on the MEASURED coordinate."""
        for _ in range(20):
            err = target - world.cam()
            if abs(err) < 0.02:
                return
            world.move(world.stage() + err / world.gain)

    reg.add(Settable("cam.lx", "Laser on sample X", "um", (-60, 60), place, world.cam))
    reg.add(Settable("dummy.row", "Row", "", (0, 10), lambda v: None, lambda: 0.0))
    cam = timed("cam.laser", lambda t: {"lx": world.cam(t)}, 60.0)
    det = timed("det", lambda t: {"a": float(signal(world.cam(t)))}, 200.0)
    reg.get("cam.lx").stream, reg.get("cam.lx").stream_channel = cam, "lx"
    g = reg.add(Gettable("det.a", "A", "V", lambda: float(signal(world.cam()))))
    g.stream, g.stream_channel = det, "a"
    return reg


def _recipe(**over):
    ax = {"type": "fly", "param": "cam.lx", "start": -8.0, "stop": 12.0, "num": 41,
          "move": "stage.x", "speed": 8.0, "speed_param": "stage.speed"}
    ax.update(over)
    return Recipe(name="camfly",
                  axes=[{"type": "array", "param": "dummy.row", "values": [0, 1, 2]}, ax],
                  detectors=["det.a"], zigzag=True)


def test_flying_in_camera_coordinates_lands_every_row_in_the_right_place():
    world = World()
    reg = build(world)
    r = _recipe()
    assert r.validate(reg) == []
    log = []
    ds = run(r, reg, on_log=log.append)
    x = ds["cam.lx"].values
    assert np.allclose(x, np.linspace(-8, 12, 41))
    # all three rows -- forward, backward, forward -- show the bump where it is
    # on the SAMPLE, although the counter runs backwards, 1.2x, and drifts
    for row in ds["det.a"].values:
        assert np.nanmax(np.abs(row - signal(x))) < 0.06
    assert np.all(ds["det.a_n"].values >= 3)
    # the first guess of the direction was wrong, and it said so once
    assert sum("learned" in m for m in log) == 1
    assert world.speed == 50.0                    # the stage speed put back


def test_the_move_parameter_is_checked():
    reg = build(World())
    errs = _recipe(move="nothing").validate(reg)
    assert any("`move` parameter 'nothing' is not available" in e for e in errs)
    errs = _recipe(move="cam.lx").validate(reg)
    assert any("names the axis parameter itself" in e for e in errs)


def test_a_stage_that_does_not_move_the_camera_coordinate_is_reported():
    """Moving the wrong stage axis (KIM mounted 90 deg to the camera): the
    readback does not move, and the scan stops with a message saying so."""
    world = World(gain=0.0, drift_um_per_s=0.0)   # this stage axis does not move the image
    reg = build(world)
    r = _recipe()
    r.axes = [r.axes[1]]                     # one row is enough
    reg.get("cam.lx")._set = lambda v: None  # placement cannot succeed either
    with pytest.raises(RuntimeError, match="does not move"):
        run(r, reg)


class Rig2D:
    """Like the KIM rig: a 2-axis stage whose X moves the camera's x (reversed,
    1.2x), whose Y moves camera y, and a camera PLACEMENT that moves the stage
    at whatever speed the stage is set to -- including the slow fly speed if
    nobody put it back."""

    def __init__(self):
        self.x, self.y = World(gain=-1.2, offset=7.0, drift_um_per_s=0.0), \
            World(gain=1.0, offset=3.0, drift_um_per_s=0.0)
        self.target = [None, None]          # the camera's placement target (lx, ly)
        self.moves = []                     # (t, axis, distance, speed) of placements

    def lx(self):
        return self.x.cam()

    def ly(self):
        return self.y.cam()

    def place(self, lx=None, ly=None):
        if lx is not None:
            self.target[0] = lx
        if ly is not None:
            self.target[1] = ly
        tx = self.target[0] if self.target[0] is not None else self.lx()
        ty = self.target[1] if self.target[1] is not None else self.ly()
        for _ in range(20):
            ex, ey = tx - self.lx(), ty - self.ly()
            if abs(ex) < 0.02 and abs(ey) < 0.02:
                return
            self.moves.append((time.monotonic(), abs(ex / self.x.gain), self.x.speed))
            tx_stage = self.x.stage() + ex / self.x.gain
            ty_stage = self.y.stage() + ey / self.y.gain
            th = threading.Thread(target=self.y.move, args=(ty_stage,))
            th.start()
            self.x.move(tx_stage)
            th.join()


def signal2(x, y):
    return np.exp(-((x - 2.0) / 3.0) ** 2) * (1.0 + 0.1 * y)


def build2(rig):
    reg = Registry()

    def stage_set(v, timeout_s=None):
        rig.x.move(v)

    reg.add(Settable("kim.position_x", "X", "um", (-500, 500), stage_set, rig.x.stage))
    reg.add(Settable("kim.velocity_x", "VX", "um/s", (0.1, 500),
                     lambda v: setattr(rig.x, "speed", v), lambda: rig.x.speed))
    reg.add(Settable("camera.laser_x", "LX", "um", (-60, 60),
                     lambda v: rig.place(lx=v), rig.lx))
    reg.add(Settable("camera.laser_y", "LY", "um", (-60, 60),
                     lambda v: rig.place(ly=v), rig.ly))
    spec = timed("camera.laser", lambda t: {"x": rig.x.cam(t), "y": rig.y.cam(t)}, 60.0)
    for pid, ch in (("camera.laser_x", "x"), ("camera.laser_y", "y")):
        reg.get(pid).stream, reg.get(pid).stream_channel = spec, ch
    det = timed("pm16", lambda t: {"p": float(signal2(rig.x.cam(t), rig.y.cam(t)))}, 200.0)
    g = reg.add(Gettable("pm16.power", "P", "mW", lambda: 0.0))
    g.stream, g.stream_channel = det, "p"
    return reg


@pytest.mark.parametrize("zigzag", [False, True])
def test_rows_placed_by_camera_y_return_fast_and_fly_both_ways(zigzag):
    """From the rig, 2026-09-27: the laser crawled back to the row start at the
    FLY speed, and with zig-zag the backward rows recorded nothing."""
    rig = Rig2D()
    reg = build2(rig)
    fly_speed = 4.0
    r = Recipe(name="rig", axes=[
        {"type": "linear", "param": "camera.laser_y", "start": -2, "stop": 2, "num": 3},
        {"type": "fly", "param": "camera.laser_x", "start": -6.0, "stop": 6.0, "num": 25,
         "move": "kim.position_x", "speed": fly_speed, "speed_param": "kim.velocity_x"}],
        detectors=["pm16.power", "camera.laser_y"], zigzag=zigzag)
    assert r.validate(reg) == []
    t0 = time.monotonic()
    ds = run(r, reg)
    x = ds["camera.laser_x"].values
    for i, y in enumerate(ds["camera.laser_y"].values):  # the setpoint of the row
        row = ds["pm16.power"].values[i]
        assert np.all(ds["pm16.power_n"].values[i] >= 3), (i, ds["pm16.power_n"].values[i])
        assert np.nanmax(np.abs(row - signal2(x, y))) < 0.08, i
    # every placement move (row returns included) ran at the stage's OWN speed,
    # never at the fly speed
    assert all(speed > fly_speed for _, dist, speed in rig.moves if dist > 1.0), rig.moves
    assert rig.x.speed == 50.0
