"""One controller, many viewers (src/chopper/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; `stop` (wheel to
standby) always works; scan-core ("machine") bypasses it; a script must take
control. Control belongs to a PC, so every test client sits at its OWN PC
(identity host "user@pcN") unless the test says otherwise.

The chopper talks to no other module's service (it drives its own MC2000B), so
there is no "machine" link to test here.

Wire tests use ports 18520/18521.
"""

from __future__ import annotations

import os
import time

import pytest

from chopper.config import Config
from chopper.sim_system import build_sim_system

CMD, PUB = 18520, 18521
zmq = pytest.importorskip("zmq")


def _cfg():
    cfg = Config()
    cfg.sim.spinup_tau_s = 0.1          # a quick wheel keeps the test short
    cfg.settle.hold_s = 0.2
    cfg.hardware.poll_hz = 20.0
    return cfg


@pytest.fixture
def svc():
    from chopper.net.service import ChopperService
    ch, _ = build_sim_system(_cfg())
    s = ChopperService(ch, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from chopper.net.client import ChopperClient
    made = []

    def make(kind="gui", name="chopper GUI", pc=None):
        c = ChopperClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_phase(10.0)
    assert _raw({"cmd": "set_phase", "phase_deg": 20.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from chopper.control import ControlRefused
    a = clients(name="chopper GUI A")
    b = clients(name="chopper GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_phase(30.0)
    with pytest.raises(ControlRefused, match="chopper GUI A"):
        b.set_phase(40.0)
    with pytest.raises(ControlRefused):
        b.set_enable(True)
    with pytest.raises(ControlRefused):
        b.set_enable(False)                           # the setter: it can also START
    assert _raw({"cmd": "start"})["refused"] == "control"
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_phase", "phase_deg": 20.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and stop the wheel
    a.set_enable(True)
    assert _wait(lambda: b.status().enabled)
    b.standby()
    assert _wait(lambda: not b.status().enabled)
    assert b.info()


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from chopper.control import ControlRefused
    a = clients(name="chopper GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_phase(15.0)                              # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_phase(25.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_phase(25.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_phase(25.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="chopper GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="chopper GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "chopper GUI B"
    assert {c["name"] for c in st["clients"]} >= {"chopper GUI A", "chopper GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="chopper GUI A")
    b = clients(name="chopper GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_phase(5.0)                                   # free again


def test_the_safety_verb_is_a_describe_action(svc):
    """The suite's Control tab offers a viewer exactly the describe actions in
    control.always -- so the safety verb must be one."""
    params = {p["id"]: p for p in _raw({"cmd": "describe"})["describe"]["parameters"]}
    always = set(svc.status_payload()["control"]["always"])
    assert "stop" in always and params["stop"]["kind"] == "action"
    assert "start" not in always


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
    """The run button has two jobs: "Start" (a change: blocked in a viewer)
    and, while the wheel runs, "Stop" (the safety verb: always works)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from chopper.apps.gui import MainWindow
    a = clients(name="chopper GUI A")
    b = clients(name="chopper GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "chopper GUI A" in wb._control_bar.label.text()

        a.set_enable(False)                              # the holder parks the wheel
        t0 = time.monotonic()
        while wb.run_btn.text() != "Start" and time.monotonic() - t0 < 3:
            _pump(qapp, 0.05)
        calls = []
        monkeypatch.setattr(b, "set_enable", lambda *x: calls.append(("enable", x)))
        monkeypatch.setattr(b, "standby", lambda *x: calls.append(("standby", x)))
        assert wb.run_btn.text() == "Start"
        QTest.mouseClick(wb.run_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer

        a.set_enable(True)                               # the holder starts the wheel
        t0 = time.monotonic()
        while wb.run_btn.text() != "Stop" and time.monotonic() - t0 < 3:
            _pump(qapp, 0.05)
        assert wb.run_btn.text() == "Stop"
        QTest.mouseClick(wb.run_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("standby", ())]                # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()
        # the bars' viewer guards filter EVERY event of the application; take
        # them out, or at interpreter exit (Python half torn down) they still
        # see the events of windows other tests left open, and print tracebacks
        for w in (wa, wb):
            qapp.removeEventFilter(w._control_bar._guard)


def test_a_local_gui_has_no_control_bar(qapp):
    from chopper.apps.gui import MainWindow
    cfg = _cfg()
    ch, _ = build_sim_system(cfg)
    w = MainWindow(ch, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
