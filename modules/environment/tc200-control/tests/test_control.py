"""One controller, many viewers (src/tc200/control.py, apps/control_bar.py).

Many clients can connect to one service; the first GUI gets control, later ones
open as VIEWERS that cannot change anything, and control changes hands only
deliberately. The service enforces it; "Heater OFF" (verb `heater_off`) always
works; scan-core ("machine") bypasses it; a script must take control. Control
belongs to a PC, so every test client sits at its OWN PC (identity host
"user@pcN") unless the test says otherwise.

Wire tests use ports 18730/18731.
"""

from __future__ import annotations

import os
import time

import pytest

from tc200.config import Config
from tc200.sim_system import build_sim_system

CMD, PUB = 18730, 18731
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from tc200.net.service import Tc200Service
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    heater, sim = build_sim_system(cfg, temperature_C=30.0, setpoint_C=30.0, enabled=True,
                                   seed=2)
    s = Tc200Service(heater, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20.0)
    s.start()
    s.sim = sim                                  # for the tests: the simulated box
    yield s
    s.stop()
    time.sleep(0.2)                              # the next test binds the same ports


@pytest.fixture
def clients():
    from tc200.net.client import Tc200Client
    made = []

    def make(kind="gui", name="tc200 GUI", pc=None):
        c = Tc200Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
                        kind=kind, name=name)
        # control belongs to a PC: each test client sits at its OWN PC unless
        # the test says otherwise (all of them really run on this one)
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
    assert a.set_temperature(31.0)["ok"]
    assert _raw({"cmd": "set_temperature", "temperature_C": 32.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from tc200.control import ControlRefused
    a = clients(name="tc200 GUI A")
    b = clients(name="tc200 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_temperature(31.0)["ok"]
    with pytest.raises(ControlRefused, match="tc200 GUI A"):
        b.set_temperature(40.0)
    with pytest.raises(ControlRefused):
        b.set_enabled(True)                           # switching ON is not safety
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_temperature", "temperature_C": 33.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and switch the heater off
    assert b.info()["simulated"] is True
    assert b.get_config() is not None
    assert b.heater_off()["ok"]
    assert svc.sim.enabled is False


def test_the_safety_verb_is_a_describe_action(svc, clients):
    """The suite's Control tab offers exactly the describe actions a viewer may
    still send (status control.always), so heater_off must be one of them."""
    a = clients()
    actions = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    assert "heater_off" in actions
    assert "heater_off" in svc.control.status()["always"]
    assert "set_enabled" not in svc.control.status()["always"]


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from tc200.control import ControlRefused
    a = clients(name="tc200 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_temperature(31.0)["ok"]           # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_temperature(32.0)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_temperature(32.0)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_temperature(33.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="tc200 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="tc200 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "tc200 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"tc200 GUI A", "tc200 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="tc200 GUI A")
    b = clients(name="tc200 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_temperature(31.0)["ok"]               # free again


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

    from tc200.apps.gui import MainWindow
    a = clients(name="tc200 GUI A")
    b = clients(name="tc200 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "tc200 GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_enabled", lambda *x: calls.append(("enabled", x)))
        monkeypatch.setattr(b, "heater_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(_button(wb, "Heater ON"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Heater OFF"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("off", ())]                    # safety always works
        assert not wa._control_bar.viewer
    finally:
        # closeEvent shuts the clients down; the fixture's second shutdown is harmless
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from tc200.apps.gui import MainWindow
    cfg = Config()
    heater, _ = build_sim_system(cfg)
    w = MainWindow(heater, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
