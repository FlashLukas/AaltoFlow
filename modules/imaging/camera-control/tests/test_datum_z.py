"""Datum Z: the Z step counter set to 0 HERE (Lukas, 2026-09-29).

Why: on the rig Lukas re-zeroes Z at every image-confirmed focus (his +-20 um
guard: kim's leash box is around the datum, and on an open-loop Z the counter
drifts from the truth over a session). The camera had "Datum XY" only; the Z
datum had to be done in the kim GUI, behind the camera's back -- so the
camera's own Z bookkeeping (the step_z target, the direction-aware counter
after a Z step calibration) still referred to the old zero and the next focus
step jumped.

What is pinned here: kim's zero_counter on Z ONLY (X and Y counters
untouched), nothing moves, the Z readout is 0 at once, the next step_z starts
from the new zero (also with two step sizes), refusals while an autofocus /
Z step calibration is running or queued (their Z positions are counted from
the old zero), no datum where the Z has none (piezo rig), the verb / client /
describe action, and the button in the Focus card with its confirm dialog.
"""

from __future__ import annotations

import time

import pytest

from camera.backends.sim import SimSlipStickZ, SimZFocus
from camera.config import Config

from test_remote_kim import FakeKimService, _kim_brain, _wait, kim  # noqa: F401 (fixture)


def _steps(z):
    return list(z.link.fresh_status()["position_steps"])


def test_datum_z_zeroes_only_the_z_counter_and_the_next_step_starts_there(kim):
    svc, xy, z = kim
    brain = _kim_brain(xy, z)
    events = []
    brain._on_event = lambda level, msg: events.append(msg)
    brain.start()
    try:
        brain.step_xy(+100, -50)
        brain.set_z(12.0)                                # 600 steps
        assert _wait(lambda: _steps(z) == [100, -50, 600])
        brain.step_z(+2.0)                               # a FRESH step target (14 um) ...
        z.wait_settled()
        assert _steps(z)[2] == 700
        n_moves = len(svc.moves)
        brain.datum_z()
        assert _steps(z)[2] == 0                         # Z counter 0 here
        assert _steps(z)[:2] == [100, -50]               # X and Y untouched
        assert len(svc.moves) == n_moves                 # nothing moved to get there
        assert brain.read_z() == pytest.approx(0.0)
        # the readout (a frame already in flight may show the old Z once)
        assert _wait(lambda: abs(brain.status().z_voltage) < 1e-9, 1.0)
        # ... and the next step is from the NEW zero, not 14 + 1 (the old target)
        assert brain.step_z(+1.0) == pytest.approx(1.0)
        assert svc.moves[-1] == ("Z", 50)
        assert any("Z datum" in m for m in events)
    finally:
        brain.shutdown()


def test_datum_z_with_two_step_sizes_reanchors_the_direction_aware_counter():
    """After a Z step calibration the camera tracks Z move by move
    (DirectionalCounter); the datum must start a fresh anchor at 0, or read_z
    keeps the old offset and the next move is planned from it."""
    from camera.backends.remote_kim import KimLink, KimXYStage, KimZFocus
    svc = FakeKimService()
    link = KimLink("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=1000)
    xy, z = KimXYStage(link), KimZFocus(link, settle_timeout_s=5.0)
    xy.open(); z.open()
    brain = _kim_brain(xy, z)
    brain.start()
    try:
        z.set_step_sizes(0.04, 0.01)                     # up 0.04, down 0.01 um/step
        brain.set_z(4.0)                                 # 100 steps up
        z.wait_settled()
        brain.set_z(3.0)                                 # 100 steps down
        z.wait_settled()
        assert brain.read_z() == pytest.approx(3.0)
        brain.datum_z()
        assert brain.read_z() == pytest.approx(0.0)
        assert brain.step_z(+1.0) == pytest.approx(1.0)
        assert svc.moves[-1] == ("Z", 25)                # 1 um / 0.04 up, from 0
        z.wait_settled()
        assert brain.read_z() == pytest.approx(1.0)
    finally:
        brain.shutdown()
        z.close(); xy.close(); svc.close()


def test_datum_z_is_refused_while_an_autofocus_or_z_calibration_is_pending(kim):
    svc, xy, z = kim
    brain = _kim_brain(xy, z)          # NOT started: a request stays queued
    brain.set_z(2.0)
    z.wait_settled()
    brain.autofocus()
    with pytest.raises(RuntimeError, match="autofocus"):
        brain.datum_z()
    assert _steps(z)[2] == 100                           # nothing zeroed
    brain.kill_af()                                      # cancels the queued run
    brain.calibrate_z_steps()
    with pytest.raises(RuntimeError, match="Z step calibration"):
        brain.datum_z()
    assert _steps(z)[2] == 100
    brain.kill_af()
    brain.datum_z()                                      # nothing pending: allowed
    assert _steps(z)[2] == 0


def test_no_z_datum_on_a_z_without_a_counter():
    from camera.backends.sim import SimCamera, SimXYStage
    from camera.camera import Camera
    cfg = Config()
    xy = SimXYStage()
    z = SimZFocus()
    brain = Camera(SimCamera(xy, z), xy, z, cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 1)
        assert brain.status().z_has_datum is False
        with pytest.raises(RuntimeError, match="no datum"):
            brain.datum_z()
    finally:
        brain.shutdown()


def test_the_slip_stick_simulator_has_a_z_datum():
    z = SimSlipStickZ(z0=5.0, z_focus=5.0)
    z.move_counter(7.0)
    true = z.true_z()
    z.zero_counter()
    assert z.counter_steps() == 0 and z.read_z() == 0.0
    assert z.true_z() == true                             # the counter moves, not Z
    z.move_counter(1.0)
    assert z.true_z() == pytest.approx(true + 1.0)


def test_verb_client_and_describe():
    pytest.importorskip("zmq")
    from camera.backends.sim import SimCamera, SimXYStage
    from camera.camera import Camera
    from camera.net.client import CameraClient
    from camera.net.describe import build_manifest
    from camera.net.service import CameraService
    cfg = Config()
    xy = SimXYStage()
    z = SimSlipStickZ(z0=30.0, z_focus=30.0, vmin=-100, vmax=100)
    brain = Camera(SimCamera(xy, z), xy, z, cfg)
    brain.start()
    try:
        m = {p["id"]: p for p in build_manifest(brain)["parameters"]}
        act = m["datum_z"]
        assert act["kind"] == "action" and act.get("danger") is True
        assert "0" in act["help"] and "nothing moves" in act["help"]
        assert m["z_has_datum"]["kind"] == "indicator"
        svc = CameraService(brain, host="127.0.0.1", cmd_port=15741, pub_port=15742,
                            status_hz=20)
        svc.start()
        cli = CameraClient("127.0.0.1", 15741, 15742, timeout_ms=3000)
        cli.start()
        try:
            assert _wait(lambda: cli.status().z_has_datum)
            cli.datum_z()
            assert z.counter_steps() == 0
            assert cli.read_z() == pytest.approx(0.0)
        finally:
            cli.close()
            svc.stop()
    finally:
        brain.shutdown()


def test_the_focus_card_has_a_datum_z_button_with_a_confirm(monkeypatch):
    import os
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from camera.apps.gui import MainWindow
    from camera.camera import CameraStatus
    from camera.sim_system import build_sim_system

    app = QApplication.instance() or QApplication([])
    brain, *_ = build_sim_system(Config())
    win = MainWindow(brain, Config())
    calls = []
    try:
        assert win.b_datum_z.text().startswith("Datum Z")
        # next to the Z target / step controls: same card as the Z + button
        assert win.b_datum_z.parentWidget() is win.b_z_up.parentWidget()
        win._sync_stage(CameraStatus(stage_ok=True, z_has_datum=False))
        assert not win.b_datum_z.isEnabled() and "no datum" in win.b_datum_z.toolTip()
        win._sync_stage(CameraStatus(stage_ok=True, z_has_datum=True))
        assert win.b_datum_z.isEnabled()
        win._sync_stage(CameraStatus(stage_ok=True, z_has_datum=True, af_running=True))
        assert not win.b_datum_z.isEnabled()               # mid-run: refused anyway
        win._sync_stage(CameraStatus(stage_ok=False, z_has_datum=True))
        assert not win.b_datum_z.isEnabled()
        win._sync_stage(CameraStatus(stage_ok=True, z_has_datum=True))

        monkeypatch.setattr(win.ctrl, "datum_z", lambda: calls.append(1))
        asked = []

        def ask(parent, title, text, *a, **k):
            asked.append(text)
            return QMessageBox.No
        monkeypatch.setattr(QMessageBox, "question", ask)
        win.b_datum_z.click()
        assert calls == [] and "nothing moves" in asked[0] and "0" in asked[0]
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        win.b_datum_z.click()
        assert calls == [1]
    finally:
        win.close()
        brain.shutdown()
        app.processEvents()
