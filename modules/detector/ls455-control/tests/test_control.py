"""One controller, many viewers (src/ls455/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; scan-core
("machine") bypasses it; a script must take control. Control belongs to a PC,
so every test client sits at its OWN PC (identity host "user@pcN") unless the
test says otherwise.

A gaussmeter has no SAFETY verb (nothing to switch off or stop); a viewer may
still read everything, including `reread_probe` (it only asks the meter which
probe is plugged in).

Wire tests use ports 18420/18421.
"""

from __future__ import annotations

import os
import time

import pytest

from ls455.config import Config
from ls455.sim_system import build_sim_system

CMD, PUB = 18420, 18421
zmq = pytest.importorskip("zmq")


def _meter():
    cfg = Config()
    cfg.acquisition.settle_time_constants = 1.0       # keep the test quick
    meter, _ = build_sim_system(cfg, realtime=False)
    cfg.hardware.poll_hz = 50.0
    return meter, cfg


@pytest.fixture
def svc():
    from ls455.net.service import Ls455Service
    meter, _ = _meter()
    s = Ls455Service(meter, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)
    time.sleep(0.2)          # ZeroMQ unbinds in its own I/O thread, a moment later


@pytest.fixture
def clients():
    from ls455.net.client import Ls455Client
    made = []

    def make(kind="gui", name="ls455 GUI", pc=None):
        c = Ls455Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_acquisition(5)["ok"]
    assert _raw({"cmd": "set_acquisition", "readings": 3})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from ls455.control import ControlRefused
    a = clients(name="ls455 GUI A")
    b = clients(name="ls455 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_acquisition(5)["ok"]
    with pytest.raises(ControlRefused, match="ls455 GUI A"):
        b.set_acquisition(3)
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger: not safety
    with pytest.raises(ControlRefused):
        b.clear_zero()                                # changes every reading
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_acquisition", "readings": 3})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read -- also re-read which probe is plugged in
    assert b.reread_probe()["ok"]
    assert b.info()
    assert isinstance(b.get_sample(), dict)


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from ls455.control import ControlRefused
    a = clients(name="ls455 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_acquisition(4)["ok"]              # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_acquisition(3)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_acquisition(3)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_acquisition(5)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="ls455 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="ls455 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "ls455 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"ls455 GUI A", "ls455 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="ls455 GUI A")
    b = clients(name="ls455 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_acquisition(5)["ok"]                  # free again


# --------------------------------------------------------------------------- #
# the GUI
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
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

    from ls455.apps.gui import MainWindow
    a = clients(name="ls455 GUI A")
    b = clients(name="ls455 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "ls455 GUI A" in wb._control_bar.label.text()

        wb.timer.stop()                                  # keep the buttons enabled for the click
        wb.acq_btn.setEnabled(True)
        wb.probe_btn.setEnabled(True)
        calls = []
        monkeypatch.setattr(b, "acquire", lambda *x: calls.append(("acquire", x)))
        monkeypatch.setattr(b, "reread_probe", lambda *x: calls.append(("reread_probe", x)))
        QTest.mouseClick(wb.acq_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(wb.probe_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("reread_probe", ())]           # only reads: allowed
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_refusal_goes_to_the_log_not_a_crash(qapp, svc, clients):
    """Control can move to another PC between the guard's check and the click:
    the service's refusal then shows in the status log."""
    from ls455.apps.gui import MainWindow
    a = clients(name="ls455 GUI A")
    b = clients(name="ls455 GUI B")
    a.take_control()
    wb = MainWindow(b, b.cfg, remote=True)
    try:
        wb._call(b.set_acquisition, 3)
        assert "read-only" in wb.log.toPlainText()
    finally:
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from ls455.apps.gui import MainWindow
    meter, cfg = _meter()
    w = MainWindow(meter, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
        meter.shutdown()
