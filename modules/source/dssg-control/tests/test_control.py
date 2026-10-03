"""One controller, many viewers (src/dssg/control.py, apps/control_bar.py).

Many clients can connect to one service; the first GUI gets control, later
ones open as VIEWERS that cannot change anything, and control changes hands
only deliberately. The service enforces it; "RF Off" (verb `rf_off`) always
works; scan-core ("machine") bypasses it; a script must take control. Control
belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

dssg talks to no other module's service, so there is no machine link to test.

Wire tests use ports 18120/18121.
"""

from __future__ import annotations

import os
import time

import pytest

from dssg.config import Config
from dssg.sim_system import build_sim_system

CMD, PUB = 18120, 18121
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from dssg.net.service import DssgService
    gen, _ = build_sim_system(Config())
    s = DssgService(gen, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from dssg.net.client import DssgClient
    made = []

    def make(kind="gui", name="dssg GUI", pc=None):
        c = DssgClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_frequency(1.0e9)
    assert _raw({"cmd": "set_power", "power_dBm": -20.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from dssg.control import ControlRefused
    a = clients(name="dssg GUI A")
    b = clients(name="dssg GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_frequency(2.0e9)
    with pytest.raises(ControlRefused, match="dssg GUI A"):
        b.set_frequency(1.0e9)
    with pytest.raises(ControlRefused):
        b.set_rf(True)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_power", "power_dBm": -25.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and switch the RF off
    a.set_rf(True)
    b.rf_off()
    assert _wait(lambda: b.status().rf_on is False)
    assert b.info()


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from dssg.control import ControlRefused
    a = clients(name="dssg GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_frequency(1.5e9)                         # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_power(-15.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_power(-15.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_power(-15.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="dssg GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="dssg GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "dssg GUI B"
    assert {c["name"] for c in st["clients"]} >= {"dssg GUI A", "dssg GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="dssg GUI A")
    b = clients(name="dssg GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_frequency(1.2e9)                             # free again


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

    from dssg.apps.gui import MainWindow
    a = clients(name="dssg GUI A")
    b = clients(name="dssg GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "dssg GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_rf", lambda *x: calls.append(("rf", x)))
        monkeypatch.setattr(b, "rf_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(wb.rf_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "RF Off"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("off", ())]                    # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from dssg.apps.gui import MainWindow
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    w = MainWindow(gen, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
