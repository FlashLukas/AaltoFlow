"""Regression tests from the deep cleaning of 2026-09-28.

Each test was written FIRST, failed on the code as it was, and passes after the
fix. One bug per test; the docstring says what went wrong and why it mattered.
All offline: fake services on random free ports, no hardware.
"""

from __future__ import annotations

import json
import os
import threading
import time
import types

import numpy as np
import pytest
import zmq

from camera import camera as camera_mod
from camera.backends.remote_kim import KimLink, KimXYStage, KimZFocus
from camera.backends.sim import SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system

from test_backup_patterns import ScrollingCamera
from test_remote_kim import FakeKimService


def _wait(cond, timeout=5.0, poll=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


# --------------------------------------------------------------------------- #
# 1. a tracking reset must not be undone by the frame that was matching
# --------------------------------------------------------------------------- #
def test_switching_tracking_off_mid_frame_is_not_undone(monkeypatch):
    """set_tracking(False) arriving while a frame is inside matchTemplate.

    The frame used to write its anchor back AFTER the reset, so the camera
    "remembered" the old position. Switched on again, it searched only a small
    box there -- and a sample moved meanwhile was never found again.
    """
    cfg = Config()
    cfg.hardware.use_z = False
    cam = ScrollingCamera()
    brain = Camera(cam, SimXYStage(), SimZFocus(), cfg)
    brain._process()                                   # a frame to draw on
    brain.capture_reference((200, 240, 60, 60))
    brain.set_tracking(True)
    brain._process()
    assert brain.status().match_found

    real = camera_mod.V.match_template
    calls = []

    def racing(*a, **k):
        if not calls:
            brain.set_tracking(False)                  # the request thread, mid-frame
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(camera_mod.V, "match_template", racing)
    brain._process()
    monkeypatch.setattr(camera_mod.V, "match_template", real)
    assert brain._last_template_xy is None, "the stale frame wrote its anchor back"

    # the consequence: the sample moves while tracking is off (150 px, more
    # than the 100 px safety box), then tracking is switched on again
    cam.ox -= 150
    brain.set_tracking(True)
    for _ in range(3):
        brain._process()
    assert brain.status().match_found, "tracking never relocked"
    assert abs(brain.status().template_x - 350) < 2


# --------------------------------------------------------------------------- #
# 2. Kill AF between "engine took the request" and "run starts"
# --------------------------------------------------------------------------- #
def _af_system():
    cfg = Config()
    cfg.autofocus.mechanism = "edges"          # no spot calibration needed
    cfg.autofocus.steps = 3
    cfg.autofocus.averages_per_level = 1
    cfg.hardware.z_step_time_ms = 0.0
    brain, cam, xy, z = build_sim_system(cfg)
    return brain


def test_kill_pressed_just_as_the_engine_takes_the_request_is_not_lost():
    """The run used to clear the kill event at its start, wiping out a Kill
    pressed after the engine had taken the request -- the run went ahead."""
    brain = _af_system()
    brain.backend.open(); brain.xy.open(); brain.z.open()
    n = brain.autofocus()
    with brain._lock:                                  # what the engine does
        req, brain._af_request = brain._af_request, None
    brain.kill_af()                                    # ...Kill lands right here
    brain._do_autofocus(req)
    s = brain.status()
    assert (s.af_id, s.af_running, s.af_error) == (n, False, "killed")


def test_a_kill_pressed_while_idle_does_not_kill_the_next_run():
    brain = _af_system()
    brain.cfg.camera.frame_rate = 200.0
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        brain.kill_af()                                # nothing running: no effect
        n = brain.autofocus()
        assert _wait(lambda: brain.status().af_id == n and not brain.status().af_running,
                     timeout=20.0)
        assert brain.status().af_error == "OK"
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. describe's Z settle tolerance vs kim's whole steps
# --------------------------------------------------------------------------- #
def test_z_settle_tolerance_covers_kims_step_rounding():
    """kim rounds a um target to whole steps (0.02 um): 0.25 um -> 12 steps ->
    0.24 um. The 0.01 um echo tolerance could never be met, so a Z scan at
    such levels waited out every point's timeout."""
    svc = FakeKimService()
    link = KimLink("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=1000)
    xy, z = KimXYStage(link), KimZFocus(link, settle_timeout_s=5.0)
    cam = ScrollingCamera()
    brain = Camera(cam, xy, z, Config())
    xy.open(); z.open()
    try:
        assert _wait(lambda: link.available()[0])
        zdesc = next(p for p in build_manifest(brain)["parameters"] if p["id"] == "z")
        tol = zdesc["settle"]["tol"]
        for target in (0.25, 0.03, -1.17):
            brain.set_z(target)
            z.wait_settled()
            got = z.read_z()
            # what scan-core's `echoes` policy checks
            assert abs(got - target) <= tol, (target, got, tol)
    finally:
        z.close(); xy.close(); svc.close()


def test_z_tolerance_stays_as_before_without_a_step_size():
    brain, *_ = build_sim_system(Config())
    zdesc = next(p for p in build_manifest(brain)["parameters"] if p["id"] == "z")
    assert zdesc["settle"]["tol"] == pytest.approx(1e-2)


# --------------------------------------------------------------------------- #
# 4. a refused Z / XY command must raise, not pass for success
# --------------------------------------------------------------------------- #
class _RefusingService:
    """A REP socket that answers every command with ok: false."""

    def __init__(self):
        self._rep = zmq.Context.instance().socket(zmq.REP)
        self.port = self._rep.bind_to_random_port("tcp://127.0.0.1")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if dict(poller.poll(50)):
                req = self._rep.recv_json()
                self._rep.send_json({"ok": False, "error": f"{req.get('cmd')}: KCube I/O error"})

    def close(self):
        self._stop.set()
        self._t.join(1.0)
        self._rep.close(0)


def test_a_refused_remote_z_or_xy_command_raises():
    from camera.backends.remote_xy import RemoteXYStage
    from camera.backends.remote_z import RemoteZFocus

    svc = _RefusingService()
    z = RemoteZFocus("127.0.0.1", svc.port, timeout_ms=1000)
    xy = RemoteXYStage("127.0.0.1", svc.port, timeout_ms=1000)
    z.open(); xy.open()
    try:
        with pytest.raises(RuntimeError, match="KCube"):
            z.set_z(10.0)
        with pytest.raises(RuntimeError):
            z.read_z()                    # used to read as 0.0 V
        with pytest.raises(RuntimeError):
            xy.move_xy(1.0, 2.0)
        with pytest.raises(RuntimeError):
            xy.read_xy()                  # used to read as (0, 0)
        assert xy.moving() is False       # unchanged: unknown = not moving
    finally:
        z.close(); xy.close(); svc.close()


# --------------------------------------------------------------------------- #
# 5. the GUI client's status thread survives a malformed frame
# --------------------------------------------------------------------------- #
def test_client_status_thread_survives_a_malformed_frame():
    from camera.net.client import CameraClient

    pub = zmq.Context.instance().socket(zmq.PUB)
    pub_port = pub.bind_to_random_port("tcp://127.0.0.1")
    # a command port nobody answers: start() only tries info/get_config
    probe = zmq.Context.instance().socket(zmq.REP)
    cmd_port = probe.bind_to_random_port("tcp://127.0.0.1")
    probe.close(0)
    client = CameraClient("127.0.0.1", cmd_port, pub_port, timeout_ms=100)
    client.start()

    def send(n):
        pub.send_multipart([b"status", json.dumps({"frame_number": n}).encode()])

    try:
        assert _wait(lambda: (send(1), client.status().frame_number == 1)[1])
        pub.send(b"junk")                                   # one part, not JSON
        pub.send_multipart([b"status", b"{not json"])
        assert _wait(lambda: (send(7), client.status().frame_number == 7)[1]), \
            "the SUB thread died on the bad frame"
    finally:
        client.close()
        pub.close(0)


# --------------------------------------------------------------------------- #
# 6. IDS: a frame that fails to convert still returns its buffer
# --------------------------------------------------------------------------- #
def test_ids_grab_requeues_the_buffer_when_conversion_fails():
    from camera.backends.ids import IDSCamera

    queued = []

    class Stream:
        def WaitForFinishedBuffer(self, ms):
            return "buf"

        def QueueBuffer(self, b):
            queued.append(b)

    def bad(buffer):
        raise RuntimeError("incomplete buffer")

    cam = IDSCamera()
    cam._stream = Stream()
    cam._ext = types.SimpleNamespace(BufferToImage=bad)
    cam._ipl = types.SimpleNamespace(PixelFormatName_Mono8=1)
    with pytest.raises(RuntimeError):
        cam.grab()
    assert queued == ["buf"], "the buffer was never handed back to the camera"


# --------------------------------------------------------------------------- #
# 7. GUI "Apply settings" must not revert what changed elsewhere
# --------------------------------------------------------------------------- #
def test_apply_settings_sends_only_what_was_edited():
    """The whole form used to be sent. After the exposure was set in the live
    parameter panel (or a scan point was picked with Index X/Y, or by a scan),
    an unrelated Apply wrote the form's OLD numbers back to the camera/brain."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    brain, *_ = build_sim_system(cfg)
    brain.start()
    win = MainWindow(brain, cfg)
    try:
        # changed elsewhere, after the window was built
        brain.set_camera_feature("ExposureTime", 777.0)
        brain.set_selected_index(2, 1)
        # the user edits ONE unrelated field in each form and applies
        win._form_widgets["camera"]["extra_delay_ms"].setValue(1.0)
        win._apply_settings([("Image", cfg.image), ("Camera", cfg.camera)])
        win._form_widgets["scanning"]["overlay_size"].setValue(7)
        win._apply_settings([("Scanning", cfg.scanning)])

        assert brain.backend.get_feature("ExposureTime") == pytest.approx(777.0)
        assert (cfg.scanning.selected_index_x, cfg.scanning.selected_index_y) == (2, 1)
        # ...and the edits themselves did arrive
        assert cfg.camera.extra_delay_ms == pytest.approx(1.0)
        assert cfg.scanning.overlay_size == 7
    finally:
        win.close()
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 8. no spot calibration from the frozen snapshot of an autofocus
# --------------------------------------------------------------------------- #
def test_spot_calibration_is_refused_while_autofocus_runs():
    """During AF no frame is analysed, but frame_number still advances: the
    calibration used to average the SAME stale centroid N times and store it
    with jitter 0, as if it were a perfect measurement."""
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 150.0        # a slow sweep: ~3 s
    brain, *_ = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        brain.calibrate_spot(5)
        n = brain.autofocus()
        assert _wait(lambda: brain.status().af_error == "running")
        with pytest.raises(RuntimeError, match="autofocus"):
            brain.calibrate_spot(5)
        assert _wait(lambda: brain.status().af_id == n and not brain.status().af_running,
                     timeout=30.0)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 9. one malformed request must not take the command port down
# --------------------------------------------------------------------------- #
def test_a_request_that_is_not_json_gets_an_error_and_the_port_stays_up():
    """A REP socket must reply before it can receive again. A non-JSON request
    was skipped without a reply, and from then on the service answered NO ONE."""
    from camera.net.service import CameraService

    cmd, pub = 15696, 15697                      # non-default ports
    brain, *_ = build_sim_system(Config())
    svc = CameraService(brain, host="127.0.0.1", cmd_port=cmd, pub_port=pub)
    svc.start()
    ctx = zmq.Context.instance()

    def req(payload: bytes):
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, 2000)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://127.0.0.1:{cmd}")
        try:
            s.send(payload)
            return json.loads(s.recv())
        finally:
            s.close(0)

    try:
        bad = req(b"this is not json")
        assert bad["ok"] is False
        assert req(b"[1, 2]")["ok"] is False             # JSON, but not an object
        good = req(json.dumps({"cmd": "status"}).encode())
        assert good["ok"] is True and "status" in good
    finally:
        svc.stop()


# --------------------------------------------------------------------------- #
# 10. a jog right after an absolute move / an autofocus starts from THERE
# --------------------------------------------------------------------------- #
def test_a_jog_after_an_absolute_move_does_not_undo_it():
    """step_xy / step_z start from the last JOG target for 3 s (so quick clicks
    add up on a walking stage). An absolute move or an autofocus in between did
    not clear it, so the next click jumped back next to the old target."""
    cfg = Config()
    cfg.hardware.xy_unit = "um"
    brain, cam, xy, z = build_sim_system(cfg)
    brain.xy.open(); brain.z.open()
    x0, y0 = brain.read_xy()
    brain.step_xy(1.0, 0.0)                    # jog: target (x0 + 1, y0)
    brain.move_xy(40.0, 50.0)                  # then an absolute go-to
    assert brain.step_xy(1.0, 0.0) == pytest.approx([41.0, 50.0])

    brain.set_z(10.0)
    brain._af_finish("OK", 7.6, 7.6)           # an autofocus parked Z elsewhere
    z.set_z(7.6)
    assert brain.step_z(0.25) == pytest.approx(7.85)


# --------------------------------------------------------------------------- #
# 11. a "%" in a text setting survives Save and Load
# --------------------------------------------------------------------------- #
def test_config_with_a_percent_sign_saves_and_loads(tmp_path):
    """configparser's default interpolation made save_config raise on any "%"."""
    from camera.config import load_config, save_config

    cfg = Config()
    cfg.image.save_path = str(tmp_path / "run 100% power")
    path = str(tmp_path / "camera.ini")
    save_config(cfg, path)
    assert load_config(path).image.save_path == cfg.image.save_path
