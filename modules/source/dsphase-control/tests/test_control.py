"""One controller, many viewers (src/dsphase/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; "RF Output Off"
(verb `output_off`) always works; scan-core ("machine") bypasses it; a script
must take control. Control belongs to a PC, so every test client sits at its
OWN PC (identity host "user@pcN") unless the test says otherwise.

dsphase talks to no other module's service (it drives its own PS6000L over a
serial port), so there is no "machine" link to test here.

Wire tests use ports 18510/18511.
"""

from __future__ import annotations

import os
import time

import pytest

from dsphase.config import Config
from dsphase.sim_system import build_sim_system

CMD, PUB = 18510, 18511
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from dsphase.net.service import DsphaseService
    brain, _ = build_sim_system(Config())
    s = DsphaseService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from dsphase.net.client import DsphaseClient
    made = []

    def make(kind="gui", name="dsphase GUI", pc=None):
        c = DsphaseClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_phase(45.0)
    assert _raw({"cmd": "set_attenuation", "attenuation_dB": 3.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from dsphase.control import ControlRefused
    a = clients(name="dsphase GUI A")
    b = clients(name="dsphase GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_phase(90.0)
    with pytest.raises(ControlRefused, match="dsphase GUI A"):
        b.set_phase(0.0)
    with pytest.raises(ControlRefused):
        b.set_output(True)
    with pytest.raises(ControlRefused):
        b.set_output(False)                           # the setter: it can also switch ON
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_attenuation", "attenuation_dB": 3.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and switch the RF output off
    a.set_output(True)
    assert _wait(lambda: b.status().output_on is True)
    b.output_off()
    assert _wait(lambda: b.status().output_on is False)
    assert b.info()


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from dsphase.control import ControlRefused
    a = clients(name="dsphase GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_phase(30.0)                              # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_attenuation(6.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_attenuation(6.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_attenuation(6.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="dsphase GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="dsphase GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "dsphase GUI B"
    assert {c["name"] for c in st["clients"]} >= {"dsphase GUI A", "dsphase GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="dsphase GUI A")
    b = clients(name="dsphase GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_phase(10.0)                                  # free again


def test_the_safety_verb_is_a_describe_action(svc):
    """The suite's Control tab offers a viewer exactly the describe actions in
    control.always -- so the safety verb must be one."""
    params = {p["id"]: p for p in _raw({"cmd": "describe"})["describe"]["parameters"]}
    always = set(svc.status_payload()["control"]["always"])
    assert "output_off" in always and params["output_off"]["kind"] == "action"


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

    from dsphase.apps.gui import MainWindow
    a = clients(name="dsphase GUI A")
    b = clients(name="dsphase GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "dsphase GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_output", lambda *x: calls.append(("out", x)))
        monkeypatch.setattr(b, "output_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(wb.out_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "RF Output Off"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("off", ())]                    # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from dsphase.apps.gui import MainWindow
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    w = MainWindow(brain, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
