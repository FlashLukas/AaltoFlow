"""One controller, many viewers (src/kim/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; STOP always works;
the camera / scan-core ("machine") bypass it; a script must take control.

Wire tests use ports 15780/15781.
"""

from __future__ import annotations

import os
import time

import pytest

from kim.config import Config
from kim.sim_system import build_sim_system

CMD, PUB = 15780, 15781
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from kim.net.service import KimService
    brain, _ = build_sim_system(Config())
    s = KimService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()


@pytest.fixture
def clients():
    from kim.net.client import KimClient
    made = []

    def make(kind="gui", name="kim GUI"):
        c = KimClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
                      kind=kind, name=name)
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def _raw(req: dict) -> dict:
    """A request from something that does not say who it is (an old script)."""
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(f"tcp://127.0.0.1:{CMD}")
    try:
        s.send_json(req)
        return s.recv_json()
    finally:
        s.close(0)


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_without_a_holder_everything_works_as_before(svc, clients):
    a = clients()
    assert a.move_steps("X", 10) == 10
    assert _raw({"cmd": "move_steps", "axis": "X", "delta": 5})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from kim.control import ControlRefused
    a = clients(name="kim GUI A")
    b = clients(name="kim GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.move_steps("X", 20)
    with pytest.raises(ControlRefused, match="kim GUI A"):
        b.move_steps("X", 20)
    with pytest.raises(ControlRefused):
        b.set_config({})                              # settings too
    r = _raw({"cmd": "move_steps", "axis": "X", "delta": 5})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and STOP
    b.stop_all()
    assert b.info()["axes"] == ["X", "Y", "Z"]
    assert b.get_config()


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from kim.control import ControlRefused
    a = clients(name="kim GUI A")
    a.take_control()
    cam = clients(kind="machine", name="camera")
    cam.move_steps("Z", 3)                            # the camera's autofocus goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.move_steps("X", 1)
    assert script.take_control(force=True)            # deliberate, visible
    script.move_steps("X", 1)
    with pytest.raises(ControlRefused, match="notebook"):
        a.move_steps("X", 1)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="kim GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="kim GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "kim GUI B"
    assert {c["name"] for c in st["clients"]} >= {"kim GUI A", "kim GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="kim GUI A")
    b = clients(name="kim GUI B")
    a.take_control()
    a.close()                                          # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.move_steps("X", 2)                               # free again


# --------------------------------------------------------------------------- #
# the GUI
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _button(win, text):
    from PySide6.QtWidgets import QPushButton
    for b in win.findChildren(QPushButton):
        if b.text() == text and b.isVisible():
            return b
    raise AssertionError(f"no visible button {text!r}")


def _pump(qapp, s=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < s:
        qapp.processEvents()
        time.sleep(0.02)


def test_gui_first_window_controls_second_is_a_viewer(qapp, svc, clients, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    from kim.apps.gui import MainWindow
    a = clients(name="kim GUI A")
    b = clients(name="kim GUI B")
    wa = MainWindow(a, Config(), remote=True)
    wb = MainWindow(b, Config(), remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert wa._control_bar.isVisible() and "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "kim GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "move_to_um", lambda *x: calls.append(("move", x)))
        monkeypatch.setattr(b, "zero_counter_all", lambda *x: calls.append(("datum", x)))
        monkeypatch.setattr(b, "stop_all", lambda *x: calls.append(("stop", x)))
        QTest.mouseClick(_button(wb, "Datum all"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "STOP"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("stop", ())]                   # safety always works

        # a local-only widget (the controller's window) is not affected
        assert not wa._control_bar.viewer

        # Take control: asks first, then B controls and A becomes the viewer
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *x, **k: QMessageBox.StandardButton.Yes)
        QTest.mouseClick(wb._control_bar.btn_take, Qt.MouseButton.LeftButton)
        _pump(qapp)
        assert b.has_control()
        assert _wait(lambda: (qapp.processEvents(), wa._control_bar.refresh(),
                              wa._control_bar.viewer)[-1])
        QTest.mouseClick(_button(wb, "Datum all"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert ("datum", ()) in calls                    # now it goes through
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from kim.apps.gui import MainWindow
    brain, _ = build_sim_system(Config())
    w = MainWindow(brain, Config(), remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
