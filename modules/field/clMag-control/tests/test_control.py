"""One controller, many viewers (src/clMag/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; "Ramp to Zero"
(verb `ramp_to_zero`) always works; scan-core ("machine") bypasses it; a script
must take control. Control belongs to a PC, so every test client sits at its
OWN PC (identity host "user@pcN") unless the test says otherwise.

Wire tests use ports 17900/17901.
"""

from __future__ import annotations

import os
import time

import pytest

from clMag.config import Config
from clMag.sim_system import build_sim_system

CMD, PUB = 17900, 17901
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from clMag.net.service import ClMagService
    ctrl, *_ = build_sim_system(Config())
    s = ClMagService(ctrl, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from clMag.net.client import ClMagClient
    made = []

    def make(kind="gui", name="clMag GUI", pc=None):
        c = ClMagClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
                        kind=kind, name=name)
        c.identity["host"] = f"user@{pc or f'pc{len(made)}'}"
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.shutdown()


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
    assert a.set_current(0.2) is not None
    assert _raw({"cmd": "set_current", "current_A": 0.1})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from clMag.control import ControlRefused
    a = clients(name="clMag GUI A")
    b = clients(name="clMag GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_current(0.3) is not None
    with pytest.raises(ControlRefused, match="clMag GUI A"):
        b.set_field(10.0)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    with pytest.raises(ControlRefused):
        b.aux_set_ao("Dev1/ao0", 1.0)                 # and the AUX outputs
    r = _raw({"cmd": "set_current", "current_A": 0.1})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and ramp the magnet to zero
    assert b.ramp_to_zero() is not None
    assert b.info()["n_points"] > 0
    assert b.get_calibration() is not None
    assert isinstance(b.aux_read_ai("Dev1/ai1"), float)


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from clMag.control import ControlRefused
    a = clients(name="clMag GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_current(0.2) is not None          # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_current(0.1)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_current(0.1) is not None
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_current(0.1)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="clMag GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="clMag GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "clMag GUI B"
    assert {c["name"] for c in st["clients"]} >= {"clMag GUI A", "clMag GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="clMag GUI A")
    b = clients(name="clMag GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_current(0.1) is not None              # free again


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

    from clMag.apps.gui import MainWindow
    a = clients(name="clMag GUI A")
    b = clients(name="clMag GUI B")
    wa = MainWindow(a, a.cfg, a.calibration, remote=True)
    wb = MainWindow(b, b.cfg, b.calibration, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "clMag GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_current", lambda *x: calls.append(("current", x)))
        monkeypatch.setattr(b, "ramp_to_zero", lambda *x: calls.append(("zero", x)))
        QTest.mouseClick(_button(wb, "Set Current"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Ramp to Zero  &  Stop"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("zero", ())]                   # safety always works
        assert not wa._control_bar.viewer
    finally:
        # closeEvent shuts the clients down; the fixture's second shutdown is harmless
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from clMag.apps.gui import MainWindow
    cfg = Config()
    ctrl, *_, cal = build_sim_system(cfg)
    w = MainWindow(ctrl, cfg, cal, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
