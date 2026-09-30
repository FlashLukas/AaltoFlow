"""One controller, many viewers (src/elliptec/control.py, apps/control_bar.py).

Many clients can connect to one service; the first GUI gets control, later ones
open as VIEWERS that cannot change anything, and control changes hands only
deliberately. The service enforces it; STOP (verb `stop`, one mount or all) always works;
scan-core ("machine") bypasses it; a script must take control. Control belongs
to a PC, so every test client sits at its OWN PC (identity host "user@pcN")
unless the test says otherwise.

Wire tests use ports 18720/18721.
"""

from __future__ import annotations

import os
import time

import pytest

from elliptec.config import Config
from elliptec.sim_system import build_sim_system

CMD, PUB = 18720, 18721
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from elliptec.net.service import ElliptecService
    cfg = Config()
    cfg.axes.addresses = "0,A"                   # two mounts on one bus
    cfg.axes.names = "HWP,POL"
    cfg.sim.max_speed_deg_s = 900.0              # fast simulated mounts: short tests
    brain, _ = build_sim_system(cfg)
    s = ElliptecService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()


@pytest.fixture
def clients():
    from elliptec.net.client import ElliptecClient
    made = []

    def make(kind="gui", name="elliptec GUI", pc=None):
        c = ElliptecClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
                           kind=kind, name=name)
        # control belongs to a PC: each test client sits at its OWN PC unless
        # the test says otherwise (all of them really run on this one)
        c.identity["host"] = f"user@{pc or f'pc{len(made)}'}"
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


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
    assert a.move_rel(0, 10.0)["move_id"] >= 1
    assert _raw({"cmd": "move_rel", "axis": 0, "delta_deg": 5.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from elliptec.control import ControlRefused
    a = clients(name="elliptec GUI A")
    b = clients(name="elliptec GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.move_rel(0, 20.0)
    with pytest.raises(ControlRefused, match="elliptec GUI A"):
        b.move_rel(0, 20.0)
    with pytest.raises(ControlRefused):
        b.set_config({})                              # settings too
    with pytest.raises(ControlRefused):
        b.home(1)                                     # homing turns the mount too
    r = _raw({"cmd": "move_rel", "axis": 0, "delta_deg": 5.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and STOP
    b.stop_all()
    b.stop(1)
    assert b.info()["addresses"] == ["0", "A"]
    assert b.get_config()


def test_the_safety_verb_is_a_describe_action(svc, clients):
    """The suite's Control tab offers exactly the describe actions a viewer may
    still send (status control.always), so STOP must be one of them."""
    a = clients()
    actions = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    assert "stop" in actions
    assert "stop" in svc.control.status()["always"]


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from elliptec.control import ControlRefused
    a = clients(name="elliptec GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.move_rel(1, 3.0)                           # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.move_rel(0, 1.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.move_rel(0, 1.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.move_rel(0, 1.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="elliptec GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="elliptec GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "elliptec GUI B"
    assert {c["name"] for c in st["clients"]} >= {"elliptec GUI A", "elliptec GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="elliptec GUI A")
    b = clients(name="elliptec GUI B")
    a.take_control()
    a.close()                                          # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.move_rel(0, 2.0)                               # free again


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

    from elliptec.apps.gui import MainWindow
    a = clients(name="elliptec GUI A")
    b = clients(name="elliptec GUI B")
    wa = MainWindow(a, Config(), remote=True)
    wb = MainWindow(b, Config(), remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert wa._control_bar.isVisible() and "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "elliptec GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "home", lambda *x: calls.append(("home", x)))
        monkeypatch.setattr(b, "stop", lambda *x: calls.append(("stop", x)))
        monkeypatch.setattr(b, "stop_all", lambda *x: calls.append(("stop_all", x)))
        QTest.mouseClick(_button(wb, "Home"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Stop"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(_button(wb, "STOP ALL"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("stop", (0,)), ("stop_all", ())]   # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from elliptec.apps.gui import MainWindow
    brain, _ = build_sim_system(Config())
    w = MainWindow(brain, Config(), remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
