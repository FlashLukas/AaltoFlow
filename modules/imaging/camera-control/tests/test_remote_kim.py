"""The KIM rig: camera XY + Z through a (fake) kim-control service.

No hardware and no ``kim`` package: a small fake service speaks kim's wire
contract (REQ/REP commands + PUB status) on random free ports, with a stage that
walks at a finite step rate like the real open-loop actuators.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest
import zmq

from camera.backends.remote_kim import KimLink, KimXYStage, KimZFocus
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config
from camera.net.describe import build_manifest

UPS = 0.02                      # um per step, all axes
LIMITS = [10000, 10000, 5000]   # symmetric, steps (a 200/200/100 um leash)


class FakeKimService:
    """kim's wire contract with a stage that walks at `rate` steps/s."""

    def __init__(self, rate: float = 4000.0, asym: float = 1.0):
        self.rate = rate
        self.asym = asym            # forward/backward step-size ratio (1 = symmetric)
        self.pos = [0.0, 0.0, 0.0]
        self.target = [0, 0, 0]
        self.status_requests = 0
        self.moves: list[tuple[str, int]] = []
        self.image_moves: list[dict] = []
        self.px_calibrated = True
        self._lock = threading.Lock()
        self._t = time.monotonic()
        self._stop = threading.Event()
        ctx = zmq.Context.instance()
        self._rep = ctx.socket(zmq.REP)
        self.cmd_port = self._rep.bind_to_random_port("tcp://127.0.0.1")
        self._pub = ctx.socket(zmq.PUB)
        self.pub_port = self._pub.bind_to_random_port("tcp://127.0.0.1")
        self._threads = [threading.Thread(target=self._serve, daemon=True),
                         threading.Thread(target=self._publish, daemon=True)]
        for t in self._threads:
            t.start()

    def _advance(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        for a in range(3):
            d = self.target[a] - self.pos[a]
            step = self.rate * dt
            self.pos[a] = self.target[a] if abs(d) <= step else self.pos[a] + np.sign(d) * step

    def _status(self) -> dict:
        with self._lock:
            self._advance()
            steps = [int(round(p)) for p in self.pos]
            return {
                "position_steps": steps,
                "position_um": [s * UPS for s in steps],
                "moving": [steps[a] != self.target[a] for a in range(3)],
                "limit_lo": [-lim for lim in LIMITS],
                "limit_hi": list(LIMITS),
                "um_per_step": [UPS] * 3,
                # kim publishes the step size of each DIRECTION (measured by the
                # camera calibration, or typed in); `asym` makes them differ so a
                # jog that converts with the mean is visibly wrong.
                "um_per_step_fwd": [UPS * self.asym] * 3,
                "um_per_step_bwd": [UPS / self.asym] * 3,
                "um_per_step_src": ["camera"] * 3,
            }

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if not dict(poller.poll(50)):
                continue
            req = self._rep.recv_json()
            cmd = req.get("cmd")
            if cmd == "status":
                self.status_requests += 1
                self._rep.send_json({"ok": True, "status": self._status()})
            elif cmd == "move_to_um":
                a = "XYZ".index(req["axis"])
                steps = int(round(req["position"] / UPS))
                steps = max(-LIMITS[a], min(LIMITS[a], steps))   # kim clamps too
                with self._lock:
                    self._advance()
                    self.target[a] = steps
                self.moves.append((req["axis"], steps))
                self._rep.send_json({"ok": True, "target": steps})
            elif cmd == "move_to_step":
                a = "XYZ".index(req["axis"])
                steps = max(-LIMITS[a], min(LIMITS[a], int(req["position"])))
                with self._lock:
                    self._advance()
                    self.target[a] = steps
                self.moves.append((req["axis"], steps))
                self._rep.send_json({"ok": True, "target": steps})
            elif cmd == "zero_counter":
                a = "XYZ".index(req["axis"])
                with self._lock:
                    self._advance()
                    shift = int(round(self.pos[a]))     # the counter moves, not the stage
                    self.pos[a] -= shift
                    self.target[a] -= shift
                self._rep.send_json({"ok": True})
            elif cmd == "move_image_px":
                if not self.px_calibrated:
                    self._rep.send_json({"ok": False, "error": "RuntimeError: no camera "
                                                               "px/step calibration"})
                else:
                    self.image_moves.append(req)
                    self._rep.send_json({"ok": True, "steps": [1, 2]})
            else:
                self._rep.send_json({"ok": False, "error": f"unknown {cmd}"})

    def _publish(self):
        while not self._stop.is_set():
            self._pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.05)

    def close(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._rep.close(0)
        self._pub.close(0)


def _wait(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def kim():
    svc = FakeKimService()
    link = KimLink("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=1000)
    xy, z = KimXYStage(link), KimZFocus(link, settle_timeout_s=5.0)
    xy.open()
    z.open()
    yield svc, xy, z
    z.close()
    xy.close()
    svc.close()


def test_xy_reads_the_published_status_not_requests(kim):
    svc, xy, _ = kim
    assert _wait(lambda: xy.link._cache is not None)
    before = svc.status_requests
    for _ in range(100):             # ~5 s of camera frames at 20 fps
        xy.read_xy()
        xy.moving()
    assert svc.status_requests - before <= 2


def test_xy_move_and_live_range(kim):
    svc, xy, _ = kim
    xy.move_xy(-3.0, 5.0)
    assert _wait(lambda: not xy.moving() and xy.read_xy() == pytest.approx((-3.0, 5.0)))
    assert xy.xy_range() == ((-200.0, 200.0), (-200.0, 200.0))


def test_z_waits_for_arrival_in_micrometres(kim):
    _, _, z = kim
    assert z.z_unit() == "um"
    z.set_z(12.0)
    z.wait_settled()                 # blocks until the walk is done
    st = z.link.fresh_status()
    assert st["position_steps"][2] == 600 and not st["moving"][2]
    # read_z uses the 20 Hz cache, so it catches up within a frame or two
    assert _wait(lambda: z.read_z() == pytest.approx(12.0))


def test_z_wait_does_not_hang_on_a_clamped_target(kim):
    _, _, z = kim
    z.set_z(1e6)                     # far past the leash: kim clamps to +5000 steps
    z.wait_settled()
    assert z.link.fresh_status()["position_steps"][2] == LIMITS[2]


def test_rpc_error_is_raised(kim):
    _, xy, _ = kim
    with pytest.raises(RuntimeError, match="unknown"):
        xy.link.rpc(cmd="no_such_verb")


class _BlankCamera:
    def open(self): pass
    def close(self): pass
    def idn(self): return "blank"
    def grab(self): return np.full((48, 64), 10, np.uint8)
    def features(self): return []
    def get_feature(self, name): return None
    def set_feature(self, name, value): pass


def test_brain_uses_kim_limits_and_units(kim):
    """The camera's 0..130 um / 0..75 V piezo envelope must NOT apply on KIM."""
    _, xy, z = kim
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    brain = Camera(_BlankCamera(), xy, z, cfg)
    brain.start()
    try:
        assert brain.move_xy(-3.0, 5.0) == [-3.0, 5.0]       # negative is fine
        assert brain.move_xy(-1000.0, 0.0) == [-200.0, 0.0]  # clamped to the leash
        assert brain.set_z(500.0) == 100.0
        assert brain.set_z(-2.0) == -2.0
        assert _wait(lambda: brain.status().z_unit == "um")
        s = brain.status()
        assert (s.z_min, s.z_max) == (-100.0, 100.0)
        zp = next(p for p in build_manifest(brain)["parameters"] if p["id"] == "z")
        assert (zp["unit"], zp["min"], zp["max"]) == ("um", -100.0, 100.0)
    finally:
        brain.shutdown()


def test_xy_jog_in_steps_and_datum_on_kim(kim):
    """kim's um are nominal, so the jog speaks steps; quick clicks add up from the
    commanded target; Datum zeroes kim's X and Y counters."""
    svc, xy, z = kim
    brain = _kim_brain(xy, z)
    brain.start()
    try:
        assert brain.xy_step_unit() == "steps"
        assert brain.step_xy(+100, 0) == [100, 0]
        assert brain.step_xy(+100, -50) == [200, -50]     # from the target, mid-walk
        assert brain.step_xy(+1e9, 0) == [LIMITS[0], -50]  # kim's clamp comes back
        assert _wait(lambda: brain.status().stage_steps_x == LIMITS[0]
                     and brain.status().stage_steps_y == -50)
        s = brain.status()
        assert s.xy_has_datum and s.limits_from_stage and s.xy_step_unit == "steps"
        assert (s.x_min, s.x_max) == (-200.0, 200.0)
        brain.datum_xy()
        assert _wait(lambda: brain.status().stage_steps_x == 0
                     and brain.status().stage_steps_y == 0)
        assert brain.step_xy(+10, 0) == [10, 0]           # jog starts from the new zero
        assert _wait(lambda: xy.read_xy() == pytest.approx((0.2, 0.0)))   # PUB caught up

        # Positioner setting: the same stage in um (kim converts, um_per_step 0.02)
        brain.cfg.hardware.xy_unit = "um"
        assert brain.xy_step_unit() == "um"
        assert brain.step_xy(+1.0, -0.5) == pytest.approx([1.2, -0.5])  # 10 steps = 0.2 um
        assert _wait(lambda: brain.status().xy_step_unit == "um"
                     and brain.status().stage_steps_x == 60)
    finally:
        brain.shutdown()


def test_um_jog_uses_the_step_size_of_the_direction_it_travels():
    """A um jog on a step-counting stage must convert with kim's FORWARD or
    BACKWARD step size, not their mean: on the real rig they differ by ~40 %."""
    svc = FakeKimService(asym=2.0)          # forward 0.04 um/step, backward 0.01
    link = KimLink("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=1000)
    xy = KimXYStage(link)
    z = KimZFocus(link)
    xy.open(); z.open()
    brain = _kim_brain(xy, z)
    brain.cfg.hardware.xy_unit = "um"
    brain.start()
    try:
        assert brain.xy_step_unit() == "um"
        assert xy.steps_for_um(0, +2.0) == 50        # 2 um / 0.04
        assert xy.steps_for_um(0, -2.0) == -200      # 2 um / 0.01

        brain.step_xy(+2.0, 0)                        # forward: the small step count
        assert _wait(lambda: brain.status().stage_steps_x == 50)
        brain.step_xy(-2.0, 0)                        # backward: the big one
        assert _wait(lambda: brain.status().stage_steps_x == -150)
        assert [m for m in svc.moves if m[0] == "X"] == [("X", 50), ("X", -150)]
    finally:
        brain.shutdown()
        xy.close(); z.close(); svc.close()


def test_camera_keeps_imaging_when_kim_is_down():
    """No kim service at all: frames must keep coming, not block on timeouts."""
    ctx = zmq.Context.instance()
    probe = ctx.socket(zmq.REP)
    port = probe.bind_to_random_port("tcp://127.0.0.1")
    probe.close(0)                   # a port nothing is listening on
    link = KimLink("127.0.0.1", port, port + 1, timeout_ms=300)
    cfg = Config()
    cfg.camera.frame_rate = 50.0
    brain = Camera(_BlankCamera(), KimXYStage(link), KimZFocus(link), cfg)
    brain.start()
    try:
        # one 0.3 s timeout, then the link answers "unreachable" instantly
        assert _wait(lambda: brain.status().frame_number >= 20, timeout=4.0)
        assert brain.xy_limits() is None          # unknown -> kim clamps itself
    finally:
        brain.shutdown()


def _kim_brain(xy, z):
    cfg = Config()
    cfg.camera.frame_rate = 50.0
    cfg.stabilizer.images_to_average = 1
    cfg.stabilizer.gain = 0.5
    return Camera(_BlankCamera(), xy, z, cfg)


def test_stabiliser_moves_the_image_in_pixels_through_kim(kim):
    """On the KIM rig the correction is an IMAGE shift: -(point - spot)/steps px,
    with our image geometry attached -- no um, no axis assumption."""
    from types import SimpleNamespace

    svc, xy, z = kim
    brain = _kim_brain(xy, z)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        geo = SimpleNamespace(point_minus_spot_px=(10.0, -4.0))
        brain._stabilise_step(geo, 0.413, 0.413, brain.status())
        assert _wait(lambda: len(svc.image_moves) == 1)
        req = svc.image_moves[0]
        assert (req["dx"], req["dy"]) == (-5.0, 2.0)
        assert req["context"]["objective"] == brain.cfg.image.objective_name
        assert req["context"]["frame"] == [48, 64]
        assert svc.moves == []                          # never the um path
    finally:
        brain.shutdown()


def test_uncalibrated_kim_refuses_and_stabiliser_does_not_guess(kim):
    from types import SimpleNamespace

    svc, xy, z = kim
    svc.px_calibrated = False
    brain = _kim_brain(xy, z)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    try:
        geo = SimpleNamespace(point_minus_spot_px=(10.0, -4.0))
        for _ in range(3):
            brain._stabilise_step(geo, 0.413, 0.413, brain.status())
        assert svc.moves == [] and svc.image_moves == []
        refused = [m for lvl, m in events if "stabiliser move refused" in m]
        assert len(refused) == 1                        # rate-limited, not one per window
    finally:
        brain.shutdown()


def test_click_to_go_sends_spot_minus_click(kim):
    svc, xy, z = kim
    brain = _kim_brain(xy, z)
    brain.cfg.spot.ref_set, brain.cfg.spot.ref_x, brain.cfg.spot.ref_y = True, 320.0, 240.0
    brain.click_to_go(300.0, 250.0)
    assert _wait(lambda: len(svc.image_moves) == 1)
    assert (svc.image_moves[0]["dx"], svc.image_moves[0]["dy"]) == (20.0, -10.0)


class _RecordingZ(SimZFocus):
    """A sim Z that logs every call, optionally claiming to be open-loop."""

    def __init__(self, open_loop: bool, **kw):
        super().__init__(**kw)
        self.open_loop = open_loop
        self.log: list[tuple[str, float]] = []

    def set_z(self, volts):
        super().set_z(volts)
        self.log.append(("set", round(float(volts), 6)))

    def wait_settled(self):
        self.log.append(("wait", self.read_z()))


def _run_autofocus(open_loop: bool):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.autofocus.drive_amplitude_v = 4.0
    cfg.autofocus.steps = 5
    cfg.autofocus.averages_per_level = 1
    cfg.autofocus.approach_margin = 1.0
    cfg.hardware.z_step_time_ms = 1.0
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = _RecordingZ(open_loop, z0=8.0, z_focus=7.6)
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um)
    brain = Camera(cam, xy, z, cfg)
    brain.start()
    try:
        brain.calibrate_spot(5)          # spot_area scores the calibrated spot only
        z.log.clear()
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running, timeout=10.0)
        assert brain.status().af_error == "OK"
        return [v for kind, v in z.log if kind == "set"], z.log, brain.status()
    finally:
        brain.shutdown()


def test_open_loop_autofocus_approaches_from_below_and_waits():
    sets, log, st = _run_autofocus(open_loop=True)
    levels = [6.0, 7.0, 8.0, 9.0, 10.0]
    # detour under the first level, then the climbing sweep
    assert sets[:6] == [5.0] + levels
    # the park comes from below too: last two moves are (target - 1) then target
    assert sets[-1] == pytest.approx(st.best_focus_v, abs=1e-6)
    assert sets[-2] == pytest.approx(sets[-1] - 1.0, abs=1e-6)
    # every move is followed by a wait for arrival
    assert all(log[i + 1][0] == "wait" for i, e in enumerate(log) if e[0] == "set")


def test_closed_loop_autofocus_is_unchanged():
    sets, _, st = _run_autofocus(open_loop=False)
    assert sets[:5] == [6.0, 7.0, 8.0, 9.0, 10.0]           # no detour
    assert sets[5:] == [pytest.approx(st.best_focus_v)]      # straight to the park


# ---- the stage is off or restarting (Lukas, 2026-09-25) ----------------------

def _dead_port():
    s = zmq.Context.instance().socket(zmq.REP)
    port = s.bind_to_random_port("tcp://127.0.0.1")
    s.close(0)                       # bound once, now closed: nothing listens
    return port


def test_a_stage_that_is_off_is_reported_and_commands_fail_fast():
    link = KimLink("127.0.0.1", _dead_port(), _dead_port(), timeout_ms=300)
    xy = KimXYStage(link)
    xy.open()
    try:
        ok, why = xy.available()
        assert not ok and "not answered" in why
        with pytest.raises(TimeoutError):          # the first attempt waits once
            link.rpc(cmd="status")
        t0 = time.monotonic()
        with pytest.raises(ConnectionError, match="Reconnect stage"):
            xy.move_to_steps(10, 10)               # ... every later one fails at once
        assert time.monotonic() - t0 < 0.1
        ok, why = xy.reconnect()
        assert not ok and "still not answering" in why
    finally:
        xy.close()


def test_status_and_reconnect_follow_the_stage(kim):
    svc = kim[0]
    link = KimLink("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=300)
    link.ALIVE_S = 0.5
    cam = SimCamera(SimXYStage(), SimZFocus(), pixel_size_x_um=0.1, pixel_size_y_um=0.1)
    brain = Camera(cam, KimXYStage(link), KimZFocus(link), Config())
    brain.start()
    try:
        assert _wait(lambda: brain.status().stage_ok), "stage should be seen"
        svc._stop.set()                            # the stage service goes quiet
        assert _wait(lambda: not brain.status().stage_ok, timeout=3.0)
        assert "silent" in brain.status().stage_error
        res = brain.reconnect_stage()
        assert res["stage_ok"] is False
        # the camera keeps imaging all the while
        n = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > n + 2)
    finally:
        brain.shutdown()


def test_the_window_greys_out_the_stage_and_offers_reconnect():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow
    from camera.camera import CameraStatus
    QApplication.instance() or QApplication([])
    from camera.sim_system import build_sim_system
    brain, *_ = build_sim_system(Config())
    win = MainWindow(brain, Config())
    try:
        down = CameraStatus(stage_ok=False, stage_error="stage (kim service) silent for 3 s",
                            xy_has_datum=True)
        win._sync_stage(down)
        assert not win.b_af.isEnabled() and not win.b_x_up.isEnabled()
        assert not win.b_datum.isEnabled() and not win.chk_stab.isEnabled()
        labels = [lab.text() for lab, _b in win._stage_bars]
        assert all("silent" in t for t in labels)
        assert all(not b.isHidden() for _l, b in win._stage_bars)
        win._sync_stage(CameraStatus(stage_ok=True, xy_has_datum=True))
        assert win.b_af.isEnabled() and win.b_datum.isEnabled()
        assert all(b.isHidden() for _l, b in win._stage_bars)
    finally:
        win.close()
