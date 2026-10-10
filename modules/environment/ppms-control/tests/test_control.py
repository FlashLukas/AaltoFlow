"""One controller, many viewers (src/ppms/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; scan-core
("machine") bypasses it; a script must take control. The PPMS has NO safety
verb (net/service.py: every verb moves a setpoint, and "Go to zero" is a
deliberate field sweep), so a viewer can only watch. Control belongs to a PC,
so every test client sits at its OWN PC (identity host "user@pcN") unless the
test says otherwise.

Wire tests use ports 18804/18805.
"""

from __future__ import annotations

import os
import time

import pytest

from ppms.config import Config
from ppms.sim_system import build_sim_system

CMD, PUB = 18804, 18805
zmq = pytest.importorskip("zmq")


def _cfg() -> Config:
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    return cfg


@pytest.fixture
def svc():
    from ppms.net.service import PpmsService
    cryo, _ = build_sim_system(_cfg(), field_mT=0.0, temperature_K=300.0)
    s = PpmsService(cryo, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from ppms.net.client import PpmsClient
    made = []

    def make(kind="gui", name="ppms GUI", pc=None):
        c = PpmsClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_field(100.0)["ok"]
    assert _raw({"cmd": "set_temperature", "temperature_K": 290.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from ppms.control import ControlRefused
    a = clients(name="ppms GUI A")
    b = clients(name="ppms GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_field(200.0)["ok"]
    with pytest.raises(ControlRefused, match="ppms GUI A"):
        b.set_field(0.0)                              # "Go to zero" is not safety
    with pytest.raises(ControlRefused):
        b.set_temperature(10.0)
    with pytest.raises(ControlRefused):
        b.set_field_rate(5.0)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_field", "field_mT": 0.0})     # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    assert svc.cryo.status().setpoint_field_mT == 200.0
    # a viewer may always read
    assert b.info()["field_max_mT"] > 0
    assert b.describe()["parameters"]
    assert b.status().connected


def test_only_ramp_stops_are_safety_verbs(svc, clients):
    """Nothing but shutdown and the two sweep stops is always allowed: every
    other verb can send the magnet or the temperature anywhere; ramp_stop
    (2026-10-09) and ramp_temperature_stop (2026-10-10) only end a sweep where
    it is (see net/service.py)."""
    clients()
    assert set(svc.status_payload()["control"]["always"]) <= {
        "shutdown", "ramp_stop", "ramp_temperature_stop"}


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from ppms.control import ControlRefused
    a = clients(name="ppms GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_field(50.0)["ok"]                 # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_field(10.0)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_field(10.0)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_field(20.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="ppms GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="ppms GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "ppms GUI B"
    assert {c["name"] for c in st["clients"]} >= {"ppms GUI A", "ppms GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="ppms GUI A")
    b = clients(name="ppms GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_field(10.0)["ok"]                     # free again


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

    from ppms.apps.gui import MainWindow
    a = clients(name="ppms GUI A")
    b = clients(name="ppms GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "ppms GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_field", lambda *x: calls.append(("field", x)))
        monkeypatch.setattr(b, "set_temperature", lambda *x: calls.append(("temp", x)))
        for text in ("Set field", "Go to zero", "Set temperature"):
            QTest.mouseClick(_button(wb, text), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # all blocked in the viewer

        # the controlling window drives as before
        monkeypatch.setattr(a, "set_field", lambda *x: calls.append(("field", x)))
        QTest.mouseClick(_button(wa, "Set field"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert [c[0] for c in calls] == ["field"]
        assert not wa._control_bar.viewer
    finally:
        # closeEvent shuts the clients down; the fixture's second shutdown is harmless
        wa.close()
        wb.close()
        # delete the windows now, not at interpreter exit: then Python has
        # already torn the bars down while their app-wide input guard still
        # sees the last events (a harmless but noisy traceback)
        wa.deleteLater()
        wb.deleteLater()
        _pump(qapp, 0.1)


def test_a_local_gui_has_no_control_bar(qapp):
    from ppms.apps.gui import MainWindow
    cfg = _cfg()
    cryo, _ = build_sim_system(cfg)
    w = MainWindow(cryo, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
