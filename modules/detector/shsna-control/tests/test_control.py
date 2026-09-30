"""One controller, many viewers (src/shsna/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; `abort` always works;
scan-core ("machine") bypasses it; a script must take control. Control belongs
to a PC, so every test client sits at its OWN PC (identity host "user@pcN")
unless the test says otherwise.

And the other direction: shsna is itself a client of the signalhound service
(backends/remote_sa.py). It identifies as a MACHINE there, so a person's
analyser GUI holding control never breaks a network-analyser sweep.

Wire tests use ports 17906/17907; the fake signalhound owner 17910/17911.
"""

from __future__ import annotations

import os
import time

import pytest

from shsna.config import Config
from shsna.sim_system import build_sim_system

CMD, PUB = 17906, 17907
OWNER_CMD, OWNER_PUB = 17910, 17911
zmq = pytest.importorskip("zmq")


def _cfg():
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 201
    return cfg


@pytest.fixture
def svc():
    from shsna.net.service import ShsnaService
    sna, _ = build_sim_system(_cfg(), realtime=False, seed=4)
    s = ShsnaService(sna, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from shsna.net.client import ShsnaClient
    made = []

    def make(kind="gui", name="shsna GUI", pc=None):
        c = ShsnaClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_points(101)["ok"]
    assert _raw({"cmd": "set_start", "start_Hz": 800e6})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from shsna.control import ControlRefused
    a = clients(name="shsna GUI A")
    b = clients(name="shsna GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_points(101)["ok"]
    with pytest.raises(ControlRefused, match="shsna GUI A"):
        b.set_points(51)
    with pytest.raises(ControlRefused):
        b.clear_reference()                           # throws data away: not safety
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_start", "start_Hz": 800e6})  # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and stop
    assert b.abort()["ok"]
    assert b.info()
    assert len(b.frequencies()) > 0


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from shsna.control import ControlRefused
    a = clients(name="shsna GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_points(151)["ok"]                 # a running scan goes on
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_points(51)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_points(51)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_points(51)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="shsna GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="shsna GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "shsna GUI B"
    assert {c["name"] for c in st["clients"]} >= {"shsna GUI A", "shsna GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="shsna GUI A")
    b = clients(name="shsna GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_points(101)["ok"]                     # free again


# --------------------------------------------------------------------------- #
# shsna as a client of the signalhound service
# --------------------------------------------------------------------------- #
def test_the_owner_link_identifies_as_a_machine():
    """Every command the real backend sends to signalhound names shsna as kind
    "machine" -- and such a request passes the owner's gate while a GUI on
    another PC holds control there (the same ControlLease code the signalhound
    service runs)."""
    from fake_owner import FakeOwner
    from shsna.backends.remote_sa import RemoteSa
    from shsna.control import ControlLease, make_identity

    cfg = Config()
    hw = cfg.hardware
    hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port = "127.0.0.1", OWNER_CMD, OWNER_PUB
    hw.timeout_ms, hw.alive_s = 400, 0.5
    owner = FakeOwner(OWNER_CMD, OWNER_PUB).start()
    be = RemoteSa(cfg)
    try:
        be.open()
        assert _wait(lambda: be.health() == "")
        be.start_sweep(100e6, 200e6, 101, 0.0, 1)
        assert _wait(be.poll)
        be.fetch()
        sent = [(r, who) for r, who in zip(owner.requests, owner.identities)
                if r.get("cmd") in ("tg_sweep_acquire", "get_tg_trace")]
        assert sent
        assert all(who and who["kind"] == "machine" and who["name"] == "shsna"
                   for _, who in sent)
        req, who = sent[0]
    finally:
        be.close()
        owner.stop()

    gate = ControlLease(safety={"abort", "tg_abort"})
    person = make_identity("gui", "signalhound GUI")
    person["host"] = "user@other-pc"
    assert gate.handle({"cmd": "take_control", "client": person})["granted"]
    assert gate.handle(dict(req, client=who)) is None            # the sweep goes on
    assert gate.handle(dict(req))["refused"] == "control"        # without it: refused


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

    from shsna.apps.gui import MainWindow
    a = clients(name="shsna GUI A")
    b = clients(name="shsna GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "shsna GUI A" in wb._control_bar.label.text()

        wb.timer.stop()                                  # keep Abort enabled for the click
        wb.abort_btn.setEnabled(True)
        wb.acq_btn.setEnabled(True)
        calls = []
        monkeypatch.setattr(b, "acquire", lambda *x: calls.append(("acquire", x)))
        monkeypatch.setattr(b, "abort", lambda *x: calls.append(("abort", x)))
        QTest.mouseClick(_button(wb, "Acquire"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(_button(wb, "Abort"), Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("abort", ())]                  # safety always works
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from shsna.apps.gui import MainWindow
    cfg = _cfg()
    sna, _ = build_sim_system(cfg, realtime=False, seed=1)
    w = MainWindow(sna, cfg, remote=False)
    try:
        assert w._control_bar is None
    finally:
        w.close()
