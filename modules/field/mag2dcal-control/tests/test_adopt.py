"""ADOPT, DON'T RESET: the service reads the magnet's state at start and changes
nothing (Lukas, 2026-09-27: "all modules should read the instrument state on
startup, not to change anything").

The simulator is PRESET to what a previous run could have left behind -- a real
DAQ card keeps its last AO and DO values when a program exits -- and a recording
wrapper around it FAILS the test on any write during start(). The one write that
is allowed is the water SAFETY INTERLOCK, and there is a test that says so.

The real backend gets the same treatment with a fake `nidaqmx` module: open()
may create tasks and read, never write.
"""

import math
import sys
import types

import pytest

from mag2dcal.backends.nidaq import NidaqVectorMagnet
from mag2dcal.backends.sim import FakeClock, SimVectorMagnet
from mag2dcal.config import Config, Hardware
from mag2dcal.controller import Controller, WaterInterlockError


class Recording:
    """Wraps a backend: logs every call in order, and raises on a WRITE while
    `forbid_writes` is set -- so a write during start() fails loudly."""

    WRITES = ("write_ao", "set_enable")

    def __init__(self, inner, hide_read_output=False):
        self._inner = inner
        self._hide = hide_read_output
        self.calls = []
        self.forbid_writes = False

    def __getattr__(self, name):
        if name == "read_output" and self._hide:
            raise AttributeError(name)       # a card that cannot report its AO/DO
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args):
            if name in self.WRITES and self.forbid_writes:
                raise AssertionError(f"state-changing write during start: {name}{args}")
            self.calls.append((name, args))
            return attr(*args)
        return call

    def writes(self):
        return [c for c in self.calls if c[0] in self.WRITES]


def make(tmp_path, *, x_V=0.0, y_V=0.0, enabled=False, water=True,
         hide_read_output=False, energize_on_start=False):
    cfg = Config()
    cfg.calibration.directory = str(tmp_path)
    cfg.calibration.load_newest_on_start = False
    cfg.control.energize_on_start = energize_on_start
    # what the previous run left on the card
    cfg.sim.start_output_x_V = x_V
    cfg.sim.start_output_y_V = y_V
    cfg.sim.start_enabled = enabled
    cfg.sim.water_ok = water
    clock = FakeClock()
    sim = SimVectorMagnet(cfg.sim, cfg.hall, cfg.temperature,
                          ai_range=(cfg.hardware.ai_min_V, cfg.hardware.ai_max_V),
                          clock=clock, seed=7)
    rec = Recording(sim, hide_read_output=hide_read_output)
    ctrl = Controller(rec, cfg, clock=clock, sleep=clock.sleep)
    events = []
    ctrl._on_event = lambda level, msg: events.append((level, msg))
    return cfg, clock, ctrl, sim, rec, events


def run(ctrl, clock, seconds):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    for _ in range(int(round(seconds / dt))):
        clock.advance(dt)
        ctrl.tick()


# ------------------------------------------------------------- energized magnet

def test_start_on_an_energized_magnet_writes_nothing_and_adopts_it(tmp_path):
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.5, y_V=1.0, enabled=True)
    bx0, by0 = sim.true_field()
    assert bx0 > 25 and by0 > 15                   # it really is holding a field

    rec.forbid_writes = True
    ctrl.start(run_thread=False)
    rec.forbid_writes = False
    assert rec.writes() == []

    s = ctrl.status()
    assert s.energized and s.state == "HOLD" and s.frozen
    assert s.output_V == [1.5, 1.0]                # the drive it FOUND
    # the setpoint is the field it measured, not 0 mT
    assert abs(s.setpoint_bx_mT - bx0) < 0.3 and abs(s.setpoint_by_mT - by0) < 0.3
    assert abs(s.setpoint_field_mT - math.hypot(bx0, by0)) < 0.3
    assert abs(s.setpoint_angle_deg - math.degrees(math.atan2(by0, bx0))) < 1.0
    assert any("ENERGIZED" in m for _, m in events)


def test_an_adopted_field_is_held_still_and_becomes_stable(tmp_path):
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=-2.0, y_V=0.5, enabled=True)
    before = sim.true_field()
    ctrl.start(run_thread=False)
    run(ctrl, clock, 3.0)
    s = ctrl.status()
    assert s.state == "STABLE" and s.field_stable and s.frozen
    # not one write in 3 s of holding: the drive is exactly what it was
    assert rec.writes() == [] and sim.ao == [-2.0, 0.5] and sim.enable
    after = sim.true_field()
    assert abs(after[0] - before[0]) < 1e-9 and abs(after[1] - before[1]) < 1e-9


def test_a_new_setpoint_after_adoption_seeks_normally(tmp_path):
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.0, enabled=True)
    ctrl.start(run_thread=False)
    ctrl.set_field(60.0, 30.0)
    dt = 1.0 / cfg.control.loop_hz
    for _ in range(int(20 / dt)):
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().field_stable:
            break
    s = ctrl.status()
    assert s.field_stable and abs(s.measured_magnitude_mT - 60.0) < 1.0


def test_energize_on_start_leaves_an_energized_magnet_alone(tmp_path):
    # The opt-in switches a magnet found OFF on; it must not re-seek one that is
    # already holding a field (that would be a push, and a back-off by field_step).
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.5, enabled=True,
                                              energize_on_start=True)
    rec.forbid_writes = True
    ctrl.start(run_thread=False)
    rec.forbid_writes = False
    s = ctrl.status()
    assert s.state == "HOLD" and s.setpoint_bx_mT > 25
    run(ctrl, clock, 2.0)
    assert rec.writes() == []


# ------------------------------------------------------------------ magnet off

def test_start_on_a_magnet_that_is_off_writes_nothing(tmp_path):
    # AO left at 2 V with the amplifier disabled: harmless, and left alone.
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=2.0, enabled=False)
    rec.forbid_writes = True
    ctrl.start(run_thread=False)
    run(ctrl, clock, 2.0)                          # the idle loop writes nothing either
    rec.forbid_writes = False
    s = ctrl.status()
    assert s.state == "OFF" and not s.energized and not s.field_stable
    assert s.output_V == [2.0, 0.0] and s.setpoint_field_mT == 0.0
    assert sim.ao == [2.0, 0.0] and not sim.enable


def test_switching_on_drives_0_V_before_enabling(tmp_path):
    # A leftover AO must not be switched straight onto the coils: the first
    # write after set_output(True) is 0 V, and only then the enable line.
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=3.0, y_V=-1.0, enabled=False)
    ctrl.start(run_thread=False)
    ctrl.set_output(True)
    run(ctrl, clock, 0.1)
    w = rec.writes()
    assert w[0] == ("write_ao", (0.0, 0.0))
    assert w[1] == ("set_enable", (True,))


def test_energize_on_start_is_opt_in_for_a_magnet_found_off(tmp_path):
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, energize_on_start=True)
    ctrl.start(run_thread=False)
    run(ctrl, clock, 3.0)
    s = ctrl.status()
    assert s.energized and s.state == "STABLE" and s.setpoint_field_mT == 0.0


# -------------------------------------------------- a card that cannot report

def test_unreadable_output_state_assumes_off_and_still_writes_nothing(tmp_path):
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.0, enabled=True,
                                              hide_read_output=True)
    rec.forbid_writes = True
    ctrl.start(run_thread=False)
    rec.forbid_writes = False
    s = ctrl.status()
    assert s.state == "OFF" and rec.writes() == []
    assert any("enable line could not be read" in m for _, m in events)


def test_unreadable_ao_on_an_energized_magnet_is_estimated_without_a_write(tmp_path):
    """The enable line reads ON but the AO read-back failed (no loopback
    channel): the brain estimates the drive from the measured field, holds
    it FROZEN, warns, and writes nothing -- the magnet keeps its field."""
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.5, y_V=-0.5, enabled=True)
    real_read = sim.read_output
    sim.read_output = lambda: (None, None, real_read()[2])
    before = sim.true_field()
    rec.forbid_writes = True
    ctrl.start(run_thread=False)
    rec.forbid_writes = False
    s = ctrl.status()
    assert s.energized and s.state == "HOLD" and rec.writes() == []
    # straight-line estimate B_measured / ff (no calibration loaded here)
    ff = cfg.control.ff_mT_per_V
    assert abs(s.output_V[0] - before[0] / ff) < 0.02     # probe noise / ff
    assert abs(s.setpoint_bx_mT - before[0]) < 0.3
    assert any("could not be read back" in m for _, m in events)
    assert sim.ao == [1.5, -0.5] and sim.enable


def test_the_service_reports_the_adopted_state_over_the_wire(tmp_path):
    """End to end: status via REQ/REP shows the FOUND field, not 0 mT."""
    pytest.importorskip("zmq")
    from mag2dcal.net.client import Mag2dcalClient
    from mag2dcal.net.service import Mag2dcalService
    from mag2dcal.sim_system import build_sim_system
    cfg = Config()
    cfg.calibration.directory = str(tmp_path)
    cfg.calibration.load_newest_on_start = False
    cfg.sim.start_output_x_V, cfg.sim.start_output_y_V = 0.0, 2.0
    cfg.sim.start_enabled = True
    ctrl, sim = build_sim_system(cfg, seed=5)
    svc = Mag2dcalService(ctrl, host="127.0.0.1", cmd_port=15990, pub_port=15991)
    svc.start()
    client = Mag2dcalClient(host="127.0.0.1", cmd_port=15990, pub_port=15991)
    try:
        client.start()
        st = client._cmd({"cmd": "status"})["status"]
        assert st["energized"] is True
        assert st["state"] in ("HOLD", "STABLE")
        assert abs(st["setpoint_angle_deg"] - 90.0) < 2.0
        assert st["setpoint_field_mT"] > 25
        assert st["output_V"] == [0.0, 2.0]
    finally:
        client.shutdown()
        svc.stop()


def test_gui_entry_boxes_start_at_the_adopted_setpoint(tmp_path):
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from mag2dcal.apps.gui import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.5, y_V=1.0, enabled=True)
    ctrl.start(run_thread=False)
    win = MainWindow(ctrl, cfg)
    win._refresh()
    s = ctrl.status()
    assert abs(win.field_spin.value() - s.setpoint_field_mT) < 0.01
    assert abs(win.angle_spin.value() - s.setpoint_angle_deg) < 0.01
    assert abs(win.bx_spin.value() - s.setpoint_bx_mT) < 0.01
    # once only: a value the user types is not overwritten by the next poll
    win.field_spin.setValue(5.0)
    win._refresh()
    assert win.field_spin.value() == 5.0
    assert rec.writes() == []                 # building the GUI sent nothing
    win.close()


# ------------------------------------------------------- the safety interlock

def test_water_interlock_is_the_one_startup_write(tmp_path):
    """KEPT ON PURPOSE: no water and no bypass -> refuse to start, and the
    backend's close() backstop de-energizes a magnet a previous run left on.
    A magnet without cooling must not stay driven."""
    cfg, clock, ctrl, sim, rec, events = make(tmp_path, x_V=1.5, enabled=True, water=False)
    with pytest.raises(WaterInterlockError):
        ctrl.start(run_thread=False)
    assert not sim.enable and sim.ao == [0.0, 0.0] and not sim.is_open


# ------------------------------------------------------ the real NI backend

class _Chans:
    def __init__(self, task, kind):
        self.task, self.kind = task, kind

    def _add(self, name, **kw):
        self.task.channels.append((self.kind, name))

    add_ao_voltage_chan = add_ai_voltage_chan = add_do_chan = add_di_chan = _add


class _FakeTask:
    log = []            # every write() on any task, class-wide
    fail_readback = False

    def __init__(self, name=""):
        self.name = name
        self.channels = []
        self.ao_channels = _Chans(self, "ao")
        self.ai_channels = _Chans(self, "ai")
        self.do_channels = _Chans(self, "do")
        self.di_channels = _Chans(self, "di")
        self.timing = types.SimpleNamespace(cfg_samp_clk_timing=lambda **kw: None)

    def write(self, data):
        _FakeTask.log.append((self.name, data))

    def read(self, **kw):
        if self.name.endswith("readback"):
            if _FakeTask.fail_readback:
                raise RuntimeError("internal channels not supported")
            # the AO pins measured through the internal loopback, raw volts
            return [-1.25, 0.75]
        if self.channels and self.channels[0][0] == "do":
            return True
        return False

    def close(self):
        pass


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    mod = types.ModuleType("nidaqmx")
    mod.Task = _FakeTask
    consts = types.ModuleType("nidaqmx.constants")
    consts.AcquisitionType = types.SimpleNamespace(FINITE="finite")
    consts.TerminalConfiguration = types.SimpleNamespace(
        DEFAULT="default", RSE="rse", NRSE="nrse", DIFF="diff")
    mod.constants = consts
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", consts)
    _FakeTask.log = []
    _FakeTask.fail_readback = False
    return mod


def test_ni_open_writes_nothing_and_reads_the_state(fake_nidaqmx):
    hw = Hardware()
    hw.ao_sign_x = -1.0                       # X coil wired backwards
    be = NidaqVectorMagnet(hw)
    be.open()
    assert _FakeTask.log == []                # not one write() on any task
    x, y, en = be.read_output()
    assert (x, y, en) == (1.25, 0.75, True)   # polarity undone, like write_ao
    assert be.found_notes == []


def test_ni_open_without_ao_readback_says_so(fake_nidaqmx):
    _FakeTask.fail_readback = True
    be = NidaqVectorMagnet(Hardware())
    be.open()
    assert _FakeTask.log == []
    x, y, en = be.read_output()
    assert x is None and y is None and en is True
    assert any("AO read-back failed" in n for n in be.found_notes)
