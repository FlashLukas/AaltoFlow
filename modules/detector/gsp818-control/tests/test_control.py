"""One controller, many viewers (src/gsp818/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; the safety verbs
`abort` and `tg_off` always work; scan-core ("machine") bypasses it; a script
must take control. Control belongs to a PC, so every test client sits at its
OWN PC (identity host "user@pcN") unless the test says otherwise.

Wire tests use ports 18460/18461.
"""

from __future__ import annotations

import os
import time

import pytest

from gsp818.config import Config
from gsp818.sim_system import build_sim_system

CMD, PUB = 18460, 18461
zmq = pytest.importorskip("zmq")


def _cfg():
    cfg = Config()
    cfg.sweep.points = 401
    return cfg


@pytest.fixture
def svc():
    from gsp818.net.service import Gsp818Service
    sa, _ = build_sim_system(_cfg(), realtime=False, seed=4)
    s = Gsp818Service(sa, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
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
    from gsp818.net.client import Gsp818Client
    made = []

    def make(kind="gui", name="gsp818 GUI", pc=None):
        c = Gsp818Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_points(201)["ok"]
    assert _raw({"cmd": "set_points", "points": 301})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from gsp818.control import ControlRefused
    a = clients(name="gsp818 GUI A")
    b = clients(name="gsp818 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_points(201)["ok"]
    with pytest.raises(ControlRefused, match="gsp818 GUI A"):
        b.set_points(301)
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger: not safety
    with pytest.raises(ControlRefused):
        b.clear_reference()                           # throws data away: not safety
    with pytest.raises(ControlRefused):
        b.set_tg(False)                               # the setter can also switch ON
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_points", "points": 301})    # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and stop
    assert b.abort()["ok"]
    assert b.tg_off()["ok"]
    assert b.info()
    assert isinstance(b.get_sample(), dict)


def test_a_viewer_switches_the_tracking_generator_off(svc, clients):
    """tg_off, the NEW safety verb: RF comes out of GEN OUTPUT, and a viewer
    who sees it on must be able to switch it off -- but not on."""
    from gsp818.control import ControlRefused
    a = clients(name="gsp818 GUI A")
    b = clients(name="gsp818 GUI B")
    a.take_control()
    assert a.set_tg(True)["ok"]
    assert _wait(lambda: b.status().tg_on)
    with pytest.raises(ControlRefused):
        b.set_tg(True)
    assert b.tg_off()["ok"]
    assert _wait(lambda: not b.status().tg_on)
    assert svc.gsp818.cfg.tracking.tg_on is False


def test_the_safety_verbs_are_describe_actions(svc, clients):
    """The suite's Control tab offers a viewer exactly the describe actions
    listed in control.always -- so every safety verb must be one."""
    a = clients()
    ids = {p["id"] for p in a.describe()["parameters"] if p["kind"] == "action"}
    always = set(svc.status_payload()["control"]["always"])
    for verb in ("abort", "tg_off"):
        assert verb in ids and verb in always


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from gsp818.control import ControlRefused
    a = clients(name="gsp818 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_points(251)["ok"]                 # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_points(301)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_points(301)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_points(201)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="gsp818 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="gsp818 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "gsp818 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"gsp818 GUI A", "gsp818 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="gsp818 GUI A")
    b = clients(name="gsp818 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_points(201)["ok"]                     # free again


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

    from gsp818.apps.gui import MainWindow
    a = clients(name="gsp818 GUI A")
    b = clients(name="gsp818 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "gsp818 GUI A" in wb._control_bar.label.text()

        wb.timer.stop()                                  # keep Abort enabled for the click
        wb.abort_btn.setEnabled(True)
        wb.acq_btn.setEnabled(True)
        calls = []
        monkeypatch.setattr(b, "acquire", lambda *x: calls.append(("acquire", x)))
        monkeypatch.setattr(b, "abort", lambda *x: calls.append(("abort", x)))
        monkeypatch.setattr(b, "set_tg", lambda *x: calls.append(("set_tg", x)))
        monkeypatch.setattr(b, "tg_off", lambda *x: calls.append(("tg_off", x)))
        QTest.mouseClick(wb.acq_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(wb.abort_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("abort", ())]                  # safety always works
        assert not wa._control_bar.viewer

        # the TG button: guarded while the TG is off (a click would switch RF
        # ON), usable while it is on (a click can only switch it OFF)
        calls.clear()
        wb.tg_btn.setChecked(False)
        wb.tg_btn.setProperty("control_always", False)
        QTest.mouseClick(wb.tg_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []
        wb.tg_btn.setChecked(True)
        wb.tg_btn.setProperty("control_always", True)
        QTest.mouseClick(wb.tg_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("tg_off", ())]
    finally:
        wa.close()
        wb.close()


def test_the_tg_button_follows_the_tg_state(qapp, svc, clients):
    """_refresh marks the TG button usable-for-viewers exactly while the TG is on."""
    from gsp818.apps.gui import MainWindow
    a = clients(name="gsp818 GUI A")
    w = MainWindow(a, a.cfg, remote=True)
    try:
        assert a.set_tg(True)["ok"]
        assert _wait(lambda: (w._refresh(), bool(w.tg_btn.property("control_always")))[1])
        assert a.tg_off()["ok"]
        assert _wait(lambda: (w._refresh(), not w.tg_btn.property("control_always"))[1])
    finally:
        w.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from gsp818.apps.gui import MainWindow
    cfg = _cfg()
    sa, _ = build_sim_system(cfg, realtime=False, seed=1)
    w = MainWindow(sa, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
        sa.shutdown()
