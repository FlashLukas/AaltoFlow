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
from scan_core.registry import Gettable, Registry, Settable
from scan_core.sim_stream import SimStreamer


class World:
    """One stage (moved at a speed) and a camera that sees the sample."""

    def __init__(self, gain=-1.2, offset=7.0, drift_um_per_s=0.4):
        self.lock = threading.Lock()
        self.p0 = self.p1 = 0.0
        self.t0 = self.t1 = time.monotonic()
        self.speed = 50.0
        self.gain, self.offset, self.drift = gain, offset, drift_um_per_s
        self.t_start = time.monotonic()
        self.gen = 0

    def stage(self, t=None):
        t = time.monotonic() if t is None else t
        with self.lock:
            if t >= self.t1 or self.t1 == self.t0:
                return self.p1
            return self.p0 + (self.p1 - self.p0) * (t - self.t0) / (self.t1 - self.t0)

    def cam(self, t=None):
        """Where the laser is on the sample, as the camera measures it."""
        t = time.monotonic() if t is None else t
        return self.offset + self.drift * (t - self.t_start) + self.gain * self.stage(t)

    def move(self, target, wait=True):
        with self.lock:
            here = self.p0 + (self.p1 - self.p0) * min(
                1.0, (time.monotonic() - self.t0) / max(1e-9, self.t1 - self.t0)) \
                if self.t1 > self.t0 else self.p1
            self.p0, self.p1 = here, float(target)
            self.t0 = time.monotonic()
            self.t1 = self.t0 + abs(self.p1 - self.p0) / self.speed
            self.gen += 1
            me = self.gen
        while wait and time.monotonic() < self.t1 and self.gen == me:
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
    cam = SimStreamer("cam.laser", lambda: {"lx": world.cam()}, rate_hz=60.0)
    det = SimStreamer("det", lambda: {"a": float(signal(world.cam()))}, rate_hz=200.0)
    reg.get("cam.lx").stream, reg.get("cam.lx").stream_channel = cam.spec(), "lx"
    g = reg.add(Gettable("det.a", "A", "V", lambda: float(signal(world.cam()))))
    g.stream, g.stream_channel = det.spec(), "a"
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
