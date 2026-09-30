"""One controller, many viewers (src/cs260/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; `abort` and
`close_shutter` always work; scan-core ("machine") bypasses it; a script must
take control. Control belongs to a PC, so every test client sits at its OWN
PC (identity host "user@pcN") unless the test says otherwise.

The monochromator talks to no other module's service (it drives its own
Cornerstone 260), so there is no "machine" link to test here.

Wire tests use ports 18530/18531.
"""

from __future__ import annotations

import os
import time

import pytest

from cs260.config import Config
from cs260.sim_system import build_sim_system

CMD, PUB = 18530, 18531
zmq = pytest.importorskip("zmq")


def _cfg():
    cfg = Config()
    cfg.sim.slew_nm_per_s_at_1200 = 4000.0     # fast drive: tests, not a movie
    cfg.sim.grating_change_s = 0.3
    cfg.motion.poll_s = 0.02
    return cfg


@pytest.fixture
def svc():
    from cs260.net.service import Cs260Service
    mono, _ = build_sim_system(_cfg())
    s = Cs260Service(mono, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from cs260.net.client import Cs260Client
    made = []

    def make(kind="gui", name="cs260 GUI", pc=None):
        c = Cs260Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    a.set_wavelength(600.0)
    assert _raw({"cmd": "set_wavelength", "wavelength_nm": 550.0})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from cs260.control import ControlRefused
    a = clients(name="cs260 GUI A")
    b = clients(name="cs260 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    a.set_wavelength(600.0)
    with pytest.raises(ControlRefused, match="cs260 GUI A"):
        b.set_wavelength(500.0)
    with pytest.raises(ControlRefused):
        b.set_shutter(True)
    with pytest.raises(ControlRefused):
        b.set_shutter(False)                          # the setter: it can also OPEN
    with pytest.raises(ControlRefused):
        b.step(10)
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_wavelength", "wavelength_nm": 550.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, stop the drive and close the shutter
    b.abort()
    a.set_shutter(True)
    assert _wait(lambda: b.status().shutter_open)
    b.close_shutter()
    assert _wait(lambda: not b.status().shutter_open)
    assert b.info()


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from cs260.control import ControlRefused
    a = clients(name="cs260 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    scan.set_wavelength(650.0)                        # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_wavelength(700.0)
    assert script.take_control(force=True)            # deliberate, visible
    script.set_wavelength(700.0)
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_wavelength(700.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="cs260 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="cs260 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "cs260 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"cs260 GUI A", "cs260 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="cs260 GUI A")
    b = clients(name="cs260 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    b.set_wavelength(620.0)                            # free again


def test_the_safety_verbs_are_describe_actions(svc):
    """The suite's Control tab offers a viewer exactly the describe actions in
    control.always -- so every safety verb must be one."""
    params = {p["id"]: p for p in _raw({"cmd": "describe"})["describe"]["parameters"]}
    always = set(svc.status_payload()["control"]["always"])
    for verb in ("abort", "close_shutter"):
        assert verb in always and params[verb]["kind"] == "action"


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
    """Abort always works; the shutter button has two jobs: "Open shutter" (a
    change: blocked in a viewer) and "Close shutter" (safety: always works)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from cs260.apps.gui import MainWindow
    a = clients(name="cs260 GUI A")
    b = clients(name="cs260 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "cs260 GUI A" in wb._control_bar.label.text()

        a.set_shutter(False)                             # the holder closes it
        t0 = time.monotonic()
        while wb.shutter_btn.text() != "Open shutter" and time.monotonic() - t0 < 3:
            _pump(qapp, 0.05)
        calls = []
        monkeypatch.setattr(b, "set_shutter", lambda *x: calls.append(("shutter", x)))
        monkeypatch.setattr(b, "close_shutter", lambda *x: calls.append(("close", x)))
        monkeypatch.setattr(b, "abort", lambda *x: calls.append(("abort", x)))
        assert wb.shutter_btn.text() == "Open shutter"
        QTest.mouseClick(wb.shutter_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Abort motion"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("abort", ())]                  # safety always works

        a.set_shutter(True)                              # the holder opens it
        t0 = time.monotonic()
        while wb.shutter_btn.text() != "Close shutter" and time.monotonic() - t0 < 3:
            _pump(qapp, 0.05)
        assert wb.shutter_btn.text() == "Close shutter"
        QTest.mouseClick(wb.shutter_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls[-1] == ("close", ())                # and so does closing
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
    from cs260.apps.gui import MainWindow
    cfg = _cfg()
    mono, _ = build_sim_system(cfg)
    w = MainWindow(mono, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
