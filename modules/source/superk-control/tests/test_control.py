"""One controller, many viewers (src/superk/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; "Emission OFF"
(verb `emission_off`) always works; scan-core ("machine") bypasses it; a script must take control. Control
belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

The lost-client guard (laser.touch) now reads the "id" of that same control
identity, so the two mechanisms share one id per client.

Wire tests use ports 18220/18221.
"""

from __future__ import annotations

import os
import time

import pytest

from superk.config import Config
from superk.sim_system import build_sim_system

CMD, PUB = 18220, 18221
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from superk.net.service import SuperkService
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    cfg.hardware.poll_hz = 20.0
    laser, _ = build_sim_system(cfg)
    s = SuperkService(laser, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from superk.net.client import SuperkClient
    made = []

    def make(kind="gui", name="superk GUI", pc=None):
        c = SuperkClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_power(30.0)
    assert _raw({"cmd": "set_power", "power_pct": 20.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from superk.control import ControlRefused
    a = clients(name="superk GUI A")
    b = clients(name="superk GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_power(30.0)
    with pytest.raises(ControlRefused, match="superk GUI A"):
        b.set_power(40.0)
    with pytest.raises(ControlRefused):
        b.set_emission(True)
    with pytest.raises(ControlRefused):
        b.set_emission(False)                         # the setter, even for "off"
    with pytest.raises(ControlRefused):
        b.reset_interlock()                           # enables a later ON: not safety
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "emission_on"})                  # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read (ping included: the watchdog's "alive")...
    assert b.info()
    assert b._cmd({"cmd": "ping"})["ok"]
    # ... and switch the emission off
    a.set_emission(True)
    assert _wait(lambda: b.status().emission_on is True)
    b.emission_off()
    assert _wait(lambda: b.status().emission_on is False)


def test_safety_verbs_are_describe_actions(svc, clients):
    """The suite's Control tab offers a viewer exactly the describe actions
    that are in control.always -- so every safety verb must be one."""
    a = clients()
    actions = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    assert svc.control.safety <= actions


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from superk.control import ControlRefused
    a = clients(name="superk GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_power(35.0)                              # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_power(45.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_power(45.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_power(10.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="superk GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="superk GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "superk GUI B"
    assert {c["name"] for c in st["clients"]} >= {"superk GUI A", "superk GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="superk GUI A")
    b = clients(name="superk GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_power(20.0)                                  # free again


def test_the_lost_client_guard_reads_the_control_identity(svc, clients):
    """The emission owner is the control identity's id: the control
    heartbeat keeps a GUI's emission alive, and an old client that sends a
    bare id string is still understood."""
    from superk.net.service import _client_id
    # longer than the control heartbeat (2 s), so the heartbeat alone can feed it
    svc.laser.cfg.hardware.client_timeout_s = 3.0
    a = clients(name="superk GUI A")
    assert a.client_id == a.identity["id"]
    a._ping_s = 0                                      # only the control heartbeat
    a.set_emission(True)
    assert _wait(lambda: a.status().emission_guarded)
    time.sleep(4.0)                                    # > timeout, heartbeats every 2 s
    assert a.status().emission_on is True
    assert _client_id({"client": {"id": "abc", "kind": "gui"}}) == "abc"
    assert _client_id({"client": "old-id"}) == "old-id"
    assert _client_id({"cmd": "ping"}) is None
    a.emission_off()


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

    from superk.apps.gui import MainWindow
    a = clients(name="superk GUI A")
    b = clients(name="superk GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "superk GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_emission", lambda *x: calls.append(("em", x)))
        monkeypatch.setattr(b, "set_rf", lambda *x: calls.append(("rf", x)))
        monkeypatch.setattr(b, "emission_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(wb.rf_btn, Qt.MouseButton.LeftButton)
        QTest.mouseClick(_button(wb, "Reset interlock"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(wb.em_off_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("off", ())]                    # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from superk.apps.gui import MainWindow
    cfg = Config()
    laser, _ = build_sim_system(cfg)
    w = MainWindow(laser, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
