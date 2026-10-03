"""One controller, many viewers (src/pm16/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; "Cancel zero" (verb
`cancel_zero`) always works; scan-core ("machine") bypasses it; a script must
take control. Control belongs to a PC, so every test client sits at its OWN PC
(identity host "user@pcN") unless the test says otherwise.

Wire tests use ports 18330/18331.
"""

from __future__ import annotations

import os
import time

import pytest

from pm16.config import Config
from pm16.sim_system import build_sim_system

CMD, PUB = 18330, 18331
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from pm16.net.service import Pm16Service
    meter, _sim = build_sim_system(Config(), realtime=False, sample_period_s=0.005,
                                   zero_time_s=10.0)   # a zero that runs until cancelled
    s = Pm16Service(meter, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from pm16.net.client import Pm16Client
    made = []

    def make(kind="gui", name="pm16 GUI", pc=None):
        c = Pm16Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_wavelength(633.0)["ok"]
    assert _raw({"cmd": "set_acquisition", "readings": 20})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from pm16.control import ControlRefused
    a = clients(name="pm16 GUI A")
    b = clients(name="pm16 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_wavelength(800.0)["ok"]
    with pytest.raises(ControlRefused, match="pm16 GUI A"):
        b.set_wavelength(532.0)
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger is not safety
    with pytest.raises(ControlRefused):
        b.zero()                                      # nor is a new zero
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_auto_range", "on": False})  # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and cancel a zero started by mistake
    assert b.info()
    assert _raw({"cmd": "stream_read"})["ok"]
    a.zero()
    assert _wait(lambda: svc.status_payload().get("zeroing"))
    assert b.cancel_zero()["ok"]
    assert _wait(lambda: not svc.status_payload().get("zeroing"))
    assert "cancel_zero" in svc.status_payload()["control"]["always"]


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from pm16.control import ControlRefused
    a = clients(name="pm16 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_wavelength(1064.0)["ok"]          # a running scan goes on
    assert scan.acquire() > 0
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_acquisition(5)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_acquisition(5)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_acquisition(10)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="pm16 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="pm16 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "pm16 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"pm16 GUI A", "pm16 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="pm16 GUI A")
    b = clients(name="pm16 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_wavelength(700.0)["ok"]               # free again


# --------------------------------------------------------------------------- #
# the GUI
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(qapp, s=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < s:
        qapp.processEvents()
        time.sleep(0.02)


def test_gui_first_window_controls_second_is_a_viewer(qapp, svc, clients, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pm16.apps.gui import MainWindow
    a = clients(name="pm16 GUI A")
    b = clients(name="pm16 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "pm16 GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_range", lambda *x: calls.append(("range", x)))
        monkeypatch.setattr(b, "cancel_zero", lambda *x: calls.append(("cancel", x)))
        QTest.mouseClick(wb.range_set, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        a.zero()                                         # the holder starts a zero ...
        assert _wait(lambda: (_pump(qapp, 0.05), wb.cancel_zero_btn.isEnabled())[1])
        QTest.mouseClick(wb.cancel_zero_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("cancel", ())]                 # ... a viewer may cancel it
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from pm16.apps.gui import MainWindow
    cfg = Config()
    meter, _sim = build_sim_system(cfg, realtime=False, sample_period_s=0.005)
    w = MainWindow(meter, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
