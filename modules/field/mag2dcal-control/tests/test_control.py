"""One controller, many viewers (src/mag2dcal/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; the two SAFETY verbs
`zero` (field 0 mT) and `output_off` (ramp down + off) always work; scan-core
("machine") bypasses it; a script must take control. Control belongs to a PC,
so every test client sits at its OWN PC (identity host "user@pcN") unless the
test says otherwise.

Wire tests use ports 18802/18803.
"""

from __future__ import annotations

import os
import time

import pytest

from mag2dcal.config import Config
from mag2dcal.sim_system import build_sim_system

CMD, PUB = 18802, 18803
zmq = pytest.importorskip("zmq")


def _cfg(tmp_path) -> Config:
    cfg = Config()
    # calibrations go to a scratch folder, never into the repository
    cfg.calibration.directory = str(tmp_path)
    cfg.calibration.load_newest_on_start = False
    return cfg


@pytest.fixture
def svc(tmp_path):
    from mag2dcal.net.service import Mag2dcalService
    ctrl, _ = build_sim_system(_cfg(tmp_path), seed=4)
    s = Mag2dcalService(ctrl, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from mag2dcal.net.client import Mag2dcalClient
    made = []

    def make(kind="gui", name="mag2dcal GUI", pc=None):
        c = Mag2dcalClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_field(1.0, 0.0)
    assert _raw({"cmd": "set_angle", "angle_deg": 10.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from mag2dcal.control import ControlRefused
    a = clients(name="mag2dcal GUI A")
    b = clients(name="mag2dcal GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_output(True)
    a.set_field(2.0, 30.0)
    with pytest.raises(ControlRefused, match="mag2dcal GUI A"):
        b.set_field(5.0)
    with pytest.raises(ControlRefused):
        b.set_output(True)                            # set_output is not safety
    with pytest.raises(ControlRefused):
        b.set_water_bypass(True)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_bx", "bx_mT": 1.0})           # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, zero the field and switch the output off
    b.zero()
    b.output_off()
    assert _wait(lambda: svc.ctrl.status().state in ("RAMP_DOWN", "OFF"))
    assert svc.ctrl.status().setpoint_field_mT == 0.0
    assert b.info()["field_max_mT"] > 0
    assert b.describe()["parameters"]


def test_a_viewer_can_abort_a_calibration_with_zero(svc, clients):
    """`zero` is the panic button: it also aborts a calibration sweep (which
    drives both axes to full field) -- and a viewer may press it."""
    from mag2dcal.control import ControlRefused
    a = clients(name="mag2dcal GUI A")
    b = clients(name="mag2dcal GUI B")
    a.take_control()
    a.set_output(True)
    a.calibrate(n_per_leg=3, dwell_s=0.5, v_max=1.0)
    assert _wait(lambda: svc.ctrl.status().state == "CALIBRATE")
    with pytest.raises(ControlRefused):
        b.calibrate(n_per_leg=3)                      # not safety
    b.zero()
    assert _wait(lambda: svc.ctrl.status().state != "CALIBRATE")


def test_safety_verbs_are_describe_actions(svc, clients):
    """The suite's Control tab offers a viewer exactly the actions listed in
    control.always -- so every safety verb must be an action in describe."""
    a = clients()
    acts = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    always = set(svc.status_payload()["control"]["always"])
    assert {"zero", "output_off"} <= acts
    assert {"zero", "output_off"} <= always


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from mag2dcal.control import ControlRefused
    a = clients(name="mag2dcal GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_field(1.0, 0.0)                          # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_field(2.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_field(2.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_field(3.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="mag2dcal GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="mag2dcal GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "mag2dcal GUI B"
    assert {c["name"] for c in st["clients"]} >= {"mag2dcal GUI A", "mag2dcal GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="mag2dcal GUI A")
    b = clients(name="mag2dcal GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_field(1.0)                                   # free again


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

    from mag2dcal.apps.gui import MainWindow
    a = clients(name="mag2dcal GUI A")
    b = clients(name="mag2dcal GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "mag2dcal GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_field", lambda *x: calls.append(("field", x)))
        monkeypatch.setattr(b, "set_output", lambda *x: calls.append(("output", x)))
        monkeypatch.setattr(b, "zero", lambda *x: calls.append(("zero", x)))
        monkeypatch.setattr(b, "output_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(_button(wb, "Go"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(_button(wb, "Energize"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Zero field"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("zero", ())]                   # safety always works

        # energized: the output button now switches OFF -- the safety verb,
        # so a viewer may click it
        a.set_output(True)
        assert _wait(lambda: (_pump(qapp, 0.05), wb.output_btn.text())[1] == "Ramp down + off")
        QTest.mouseClick(_button(wb, "Ramp down + off"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("zero", ()), ("off", ())]
        assert not wa._control_bar.viewer
    finally:
        # closeEvent shuts the clients down; the fixture's second shutdown is harmless
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp, tmp_path):
    from mag2dcal.apps.gui import MainWindow
    cfg = _cfg(tmp_path)
    ctrl, _ = build_sim_system(cfg)
    w = MainWindow(ctrl, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
