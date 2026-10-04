"""One controller, many viewers (src/usb6001/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; scan-core
("machine") bypasses it; a script must take control. This general DAQ has NO
safety verb (net/service.py: no output value is safe for every setup), so a
viewer can read -- live status, read_ai / read_di -- but set nothing. Control
belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

Wire tests use ports 18808/18809.
"""

from __future__ import annotations

import os
import time

import pytest

from usb6001.sim_system import build_sim_system, demo_config

CMD, PUB = 18808, 18809
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from usb6001.net.service import Usb6001Service
    daq, _ = build_sim_system(demo_config())
    s = Usb6001Service(daq, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from usb6001.net.client import Usb6001Client
    made = []

    def make(kind="gui", name="usb6001 GUI", pc=None):
        c = Usb6001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_ao(0, 1.0) == 1.0
    assert _raw({"cmd": "set_do", "line": "p0.4", "state": True})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from usb6001.control import ControlRefused
    a = clients(name="usb6001 GUI A")
    b = clients(name="usb6001 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_ao(0, 0.5) == 0.5
    with pytest.raises(ControlRefused, match="usb6001 GUI A"):
        b.set_ao(0, 0.0)                              # 0 V is not "safe" in general
    with pytest.raises(ControlRefused):
        b.set_do("p0.4", False)
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger is not a read
    with pytest.raises(ControlRefused):
        b.save_config()
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_ao", "channel": 0, "volts": 0.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    assert svc.gen.status().ao_V[0] == 0.5
    # a viewer may always read
    assert "values" in b.read_ai()
    assert "levels" in b.read_di()
    assert isinstance(b.get_sample(), dict)
    assert b.info()["do"]
    assert b.describe()["parameters"]


def test_no_safety_verbs(svc, clients):
    """Nothing but shutdown is always allowed: a general DAQ has no output
    value that is safe for every setup (see net/service.py)."""
    clients()
    assert set(svc.status_payload()["control"]["always"]) <= {"shutdown"}


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from usb6001.control import ControlRefused
    a = clients(name="usb6001 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_ao(1, 0.25) == 0.25               # a running scan goes on
    assert scan.acquire() >= 0
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_ao(1, 0.5)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_ao(1, 0.5) == 0.5
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_ao(1, 0.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="usb6001 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="usb6001 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "usb6001 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"usb6001 GUI A", "usb6001 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="usb6001 GUI A")
    b = clients(name="usb6001 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_ao(0, 0.1) == 0.1                     # free again


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

    from usb6001.apps.gui import MainWindow
    a = clients(name="usb6001 GUI A")
    b = clients(name="usb6001 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "usb6001 GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_ao", lambda *x: calls.append(("ao", x)))
        monkeypatch.setattr(b, "read_ai", lambda *x: calls.append(("read", x)) or {})
        QTest.mouseClick(_button(wb, "Set"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Read inputs now"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("read", ())]                   # reading always works
        assert not wa._control_bar.viewer
    finally:
        # closeEvent shuts the clients down; the fixture's second shutdown is harmless
        wa.close()
        wb.close()
        # delete the windows now, not at interpreter exit (then the bars' app-wide
        # input guard can outlive their Python side: a noisy traceback)
        wa.deleteLater()
        wb.deleteLater()
        _pump(qapp, 0.1)


def test_a_local_gui_has_no_control_bar(qapp):
    from usb6001.apps.gui import MainWindow
    cfg = demo_config()
    daq, _ = build_sim_system(cfg)
    w = MainWindow(daq, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
