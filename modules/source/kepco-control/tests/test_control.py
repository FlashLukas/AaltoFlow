"""One controller, many viewers (src/kepco/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; taking the output
away (verbs `output_off` = ramp down, and `output_off_now` = no ramp) always
works; scan-core ("machine") bypasses it; a script must take control. Control
belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

Wire tests use ports 18200/18201.
"""

from __future__ import annotations

import os
import time

import pytest

from kepco.config import Config
from kepco.sim_system import build_sim_system

CMD, PUB = 18200, 18201
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from kepco.net.service import KepcoService
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0                   # quick ramps for the tests
    supply, _ = build_sim_system(cfg, seed=0)
    s = KepcoService(supply, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from kepco.net.client import KepcoClient
    made = []

    def make(kind="gui", name="kepco GUI", pc=None):
        c = KepcoClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_current(0.5)
    assert _raw({"cmd": "set_voltage_limit", "voltage_V": 5.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from kepco.control import ControlRefused
    a = clients(name="kepco GUI A")
    b = clients(name="kepco GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_current(0.5)
    with pytest.raises(ControlRefused, match="kepco GUI A"):
        b.set_current(1.0)
    with pytest.raises(ControlRefused):
        b.set_output(True)
    with pytest.raises(ControlRefused):
        b.set_output(False)                           # the setter, even for "off"
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_current", "current_A": 0.2})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read (ping included: the watchdog's "alive")...
    assert b.info()
    assert b._cmd({"cmd": "ping"})["ok"]
    # ... and take the output away: ramped, and the emergency switch-off
    a.set_output(True)
    assert _wait(lambda: b.status().output_request is True)
    b.output_off()
    assert _wait(lambda: b.status().output_request is False)
    a.set_output(True)
    assert _wait(lambda: b.status().output_request is True)
    b.output_off_now()
    assert _wait(lambda: b.status().output_request is False)


def test_safety_verbs_are_describe_actions(svc, clients):
    """The suite's Control tab offers a viewer exactly the describe actions
    that are in control.always -- so every safety verb must be one."""
    a = clients()
    actions = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    assert svc.control.safety <= actions


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from kepco.control import ControlRefused
    a = clients(name="kepco GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_current(0.3)                             # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_current(0.4)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_current(0.4)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_current(0.1)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="kepco GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="kepco GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "kepco GUI B"
    assert {c["name"] for c in st["clients"]} >= {"kepco GUI A", "kepco GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="kepco GUI A")
    b = clients(name="kepco GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    # A crashed window sends nothing at all: stop its watchdog pings too
    # (they carry its identity, so they would keep the lease alive).
    a._ping_s = 0
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_current(0.2)                                 # free again


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

    from kepco.apps.gui import MainWindow
    a = clients(name="kepco GUI A")
    b = clients(name="kepco GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "kepco GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_output", lambda *x: calls.append(("out", x)))
        monkeypatch.setattr(b, "set_mode", lambda *x: calls.append(("mode", x)))
        monkeypatch.setattr(b, "output_off", lambda *x: calls.append(("off", x)))
        monkeypatch.setattr(b, "output_off_now", lambda *x: calls.append(("kill", x)))
        # output off: the big button reads "Output ON" and is blocked
        QTest.mouseClick(wb.out_btn, Qt.MouseButton.LeftButton)
        QTest.mouseClick(wb.btn_volt, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Output off NOW (no ramp)"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("kill", ())]                   # safety always works
        # output on (by the holder): the big button now reads OFF and sends
        # the safety verb output_off, so the viewer may press it
        a.set_output(True)
        assert _wait(lambda: (_pump(qapp, 0.05) or True)
                     and wb.out_btn.text().startswith("Ramp down"))
        QTest.mouseClick(wb.out_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls[-1] == ("off", ())
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from kepco.apps.gui import MainWindow
    cfg = Config()
    supply, _ = build_sim_system(cfg)
    w = MainWindow(supply, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
