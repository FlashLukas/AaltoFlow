"""One controller, many viewers (src/sr830/control.py, apps/control_bar.py).

Lukas (2026-09-29): many clients can connect to one service; the first GUI gets
control, later ones open as VIEWERS that cannot change anything, and control
changes hands only deliberately. The service enforces it; "Outputs off" (verb
`output_off`: SINE OUT to minimum, every AUX OUT to 0 V) always works;
scan-core ("machine") bypasses it; a script must take control. Control belongs
to a PC, so every test client sits at its OWN PC (identity host "user@pcN")
unless the test says otherwise.

Wire tests use ports 18310/18311.
"""

from __future__ import annotations

import os
import time

import pytest

from sr830.config import Config
from sr830.sim_system import build_sim_system

CMD, PUB = 18310, 18311
zmq = pytest.importorskip("zmq")


@pytest.fixture
def svc():
    from sr830.net.service import Sr830Service
    cfg = Config()
    cfg.demod.time_constant = "3 ms"          # short, so acquisitions finish fast
    li, sim = build_sim_system(cfg, seed=3)
    sim.auto_gain_s = 0.1
    s = Sr830Service(li, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()
    # stop() does not wait for the socket threads; the next test binds the
    # same ports, so wait until they have closed them
    s._cmd_t.join(timeout=2.0)
    s._pub_t.join(timeout=2.0)


@pytest.fixture
def clients():
    from sr830.net.client import Sr830Client
    made = []

    def make(kind="gui", name="sr830 GUI", pc=None):
        c = Sr830Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000,
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
    assert a.set_phase(10.0)["ok"]
    assert _raw({"cmd": "set_harmonic", "harmonic": 2})["ok"]


def test_the_first_takes_control_the_second_is_a_viewer(svc, clients):
    from sr830.control import ControlRefused
    a = clients(name="sr830 GUI A")
    b = clients(name="sr830 GUI B")
    assert a.take_control() is True
    assert b.take_control() is False                  # not forced: stays a viewer
    assert a.set_sine_out(1.0)["ok"]
    assert a.set_aux_out(2, 1.5)["ok"]
    with pytest.raises(ControlRefused, match="sr830 GUI A"):
        b.set_sine_out(2.0)
    with pytest.raises(ControlRefused):
        b.acquire()                                   # a trigger is not safety
    with pytest.raises(ControlRefused):
        b.auto_gain()
    with pytest.raises(ControlRefused):
        b.apply_config()                              # settings too
    r = _raw({"cmd": "set_aux_out", "channel": 1, "volts": 3.0})   # anonymous script
    assert r["ok"] is False and r["refused"] == "control"
    # a viewer may always read, and take the outputs off the sample
    assert b.info()
    assert _raw({"cmd": "stream_read"})["ok"]
    assert b.output_off()["ok"]
    st = svc.lockin.status()
    assert st.sine_out_set_V == pytest.approx(svc.lockin.cfg.limits.sine_min_V)
    assert st.aux_out_set_V == [0.0, 0.0, 0.0, 0.0]
    assert "output_off" in svc.status_payload()["control"]["always"]


def test_output_off_is_a_describe_action(svc):
    from sr830.net.describe import build_manifest
    p = {q["id"]: q for q in build_manifest(svc.lockin)["parameters"]}["output_off"]
    assert p["kind"] == "action"


def test_machines_bypass_and_a_script_must_take_control(svc, clients):
    from sr830.control import ControlRefused
    a = clients(name="sr830 GUI A")
    a.take_control()
    scan = clients(kind="machine", name="scan-core")
    assert scan.set_phase(45.0)["ok"]                 # a running scan goes on
    assert scan.acquire() > 0
    script = clients(kind="script", name="notebook")
    with pytest.raises(ControlRefused):
        script.set_phase(0.0)
    assert script.take_control(force=True)            # deliberate, visible
    assert script.set_phase(0.0)["ok"]
    with pytest.raises(ControlRefused, match="notebook"):
        a.set_phase(5.0)


def test_a_take_over_is_announced_and_seen_in_status(svc, clients):
    a = clients(name="sr830 GUI A")
    events = []
    a._on_event = lambda level, msg: events.append((level, msg))
    b = clients(name="sr830 GUI B")
    a.take_control()
    assert b.take_control(force=True)
    assert _wait(lambda: any("took over" in m for _, m in events))
    assert _wait(lambda: not a.has_control() and b.has_control())
    st = svc.status_payload()["control"]
    assert st["holder"]["name"] == "sr830 GUI B"
    assert {c["name"] for c in st["clients"]} >= {"sr830 GUI A", "sr830 GUI B"}


def test_a_silent_holder_loses_control(svc, clients):
    svc.control.lease_s = 1.0
    a = clients(name="sr830 GUI A")
    b = clients(name="sr830 GUI B")
    a.take_control()
    a.stop_heartbeat()                                 # crashed window: no more heartbeats
    assert _wait(lambda: svc.control.status()["holder"] is None, timeout=4)
    assert b.set_phase(12.0)["ok"]                     # free again


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


def _pump(qapp, s=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < s:
        qapp.processEvents()
        time.sleep(0.02)


def test_gui_first_window_controls_second_is_a_viewer(qapp, svc, clients, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from sr830.apps.gui import MainWindow
    a = clients(name="sr830 GUI A")
    b = clients(name="sr830 GUI B")
    wa = MainWindow(a, a.cfg, remote=True)
    wb = MainWindow(b, b.cfg, remote=True)
    wa.show()
    wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not b.has_control()
        assert "You have control" in wa._control_bar.label.text()
        assert wb._control_bar.viewer and "VIEWER" in wb._control_bar.label.text()
        assert "sr830 GUI A" in wb._control_bar.label.text()

        calls = []
        monkeypatch.setattr(b, "set_frequency", lambda *x: calls.append(("freq", x)))
        monkeypatch.setattr(b, "output_off", lambda *x: calls.append(("off", x)))
        QTest.mouseClick(wb.controls.freq_set, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []                               # blocked in the viewer
        QTest.mouseClick(wb.off_btn, Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == [("off", ())]                    # safety always works
        settings = wb.inst_tab.settings
        for btn in (settings.save_btn, settings.revert_btn):
            assert wb._control_bar.guarded_input(btn) is None
        assert not wa._control_bar.viewer
    finally:
        wa.close()
        wb.close()


def test_a_local_gui_has_no_control_bar(qapp):
    from sr830.apps.gui import MainWindow
    cfg = Config()
    li, _sim = build_sim_system(cfg, seed=5)
    w = MainWindow(li, cfg, remote=False)
    try:
        assert w._control_bar is None
        w.off_btn.click()                              # works on a local brain too
        assert li.status().sine_out_set_V == pytest.approx(cfg.limits.sine_min_V)
    finally:
        w.close()
