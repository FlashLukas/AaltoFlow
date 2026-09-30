"""One controller, many viewers (src/signalhound/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; `abort` and
`tg_abort` always work; the client modules shsg / shsna and scan-core
("machine") bypass it -- a person's analyser GUI must never break the
generator or the network analyser driving the TG; a script must take control.
Control belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

Wire tests use ports 17902/17903.
"""

from __future__ import annotations

import os
import time

import pytest

from signalhound.config import Config
from signalhound.sim_system import build_sim_system

CMD, PUB = 17902, 17903
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from signalhound.net.service import SignalhoundService
    cfg = Config()
    cfg.sweep.span_Hz = 20e6
    sa, _ = build_sim_system(cfg, realtime=False, seed=4)
    s = SignalhoundService(sa, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from signalhound.net.client import SignalhoundClient
    made = []

    def make(kind="gui", name="signalhound GUI", pc=None):
        c = SignalhoundClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_center(2.0e9)["ok"]
    assert _raw({"cmd": "set_ref_level", "ref_level_dBm": -10.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from signalhound.control import ControlRefused
    a = clients(name="signalhound GUI A")
    b = clients(name="signalhound GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_center(2.0e9)["ok"]
    with pytest.raises(ControlRefused, match="signalhound GUI A"):
        b.set_center(1.0e9)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger changes the latched sample
    r = _raw({"cmd": "set_span", "span_Hz": 1e6})     # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and stop
    assert b.abort()["ok"]
    assert b.tg_abort() in (True, False)
    assert b.tg_grid(1e9, 2e9, points=11)["points"] == 11
    assert b.info()["model"] == "SA44B"
    assert len(b.frequencies()) > 0


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from signalhound.control import ControlRefused
    a = clients(name="signalhound GUI A")
    a.take_control()
    gen = clients(kind="machine", name="shsg")
    assert gen.tg_cw(True, 1.0e9, -20.0)["on"] is True   # the generator goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_rbw(10e3)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_rbw(10e3)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_rbw(10e3)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="signalhound GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="signalhound GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "signalhound GUI B"
    assert {c["name"] for c in st["clients"]} >= {"signalhound GUI A", "signalhound GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="signalhound GUI A")
    b = clients(name="signalhound GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_center(1.5e9)["ok"]                   # free again


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

    from signalhound.apps.gui import MainWindow
    a = clients(name="signalhound GUI A")
    b = clients(name="signalhound GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "signalhound GUI A" in wb._control_bar.label.text()

        wb.timer.stop()                                  # keep Abort enabled for the click
        wb.abort_btn.setEnabled(True)
        wb.acq_btn.setEnabled(True)
        calls = []
        monkeypatch.setattr(b, "acquire", lambda *x: calls.append(("acquire", x)))
        monkeypatch.setattr(b, "abort", lambda *x: calls.append(("abort", x)))
        QTest.mouseClick(_button(wb, "Acquire"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Abort"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("abort", ())]                  # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False, seed=1)
    w = MainWindow(sa, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
