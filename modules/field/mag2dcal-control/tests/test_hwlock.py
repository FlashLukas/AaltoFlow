"""One physical magnet, one service (hwlock.py), offline.

The real backend needs NI-DAQmx, which a test PC does not have. So a tiny FAKE
`nidaqmx` package is put into sys.modules: just enough of Task / channels /
timing / constants for NidaqVectorMagnet.open() and close() to run. The lock
files go to a temp folder (AALTOFLOW_LOCK_DIR), never to the real
%LOCALAPPDATA%\\AaltoFlow\\locks, so a service Lukas has running is not affected.
"""

from __future__ import annotations

import sys
import types

import pytest

from mag2dcal import hwlock
from mag2dcal.backends.nidaq import NidaqVectorMagnet, daq_devices
from mag2dcal.backends.sim import SimVectorMagnet  # noqa: F401  (import check)
from mag2dcal.config import Config, Hardware
from mag2dcal.hwlock import HardwareBusy
from mag2dcal.sim_system import build_sim_system


# ------------------------------------------------------------------ fake nidaqmx

class _Chans:
    def __init__(self, task):
        self._task = task

    def _add(self, name, **kw):
        if self._task.fail_on_add:
            raise RuntimeError("DAQmx error -200220: device identifier is invalid")
        self._task.channels.append(name)

    add_ao_voltage_chan = add_ai_voltage_chan = add_do_chan = add_di_chan = _add


class _Timing:
    def cfg_samp_clk_timing(self, **kw):
        pass


class _Task:
    fail_on_add = False
    instances: list = []

    def __init__(self, name=""):
        self.name, self.channels, self.writes, self.closed = name, [], [], False
        self.ao_channels = self.ai_channels = self.do_channels = self.di_channels = _Chans(self)
        self.timing = _Timing()
        _Task.instances.append(self)

    def read(self, **kw):
        if self.name.endswith("readback"):
            return [0.0, 0.0]
        return False

    def write(self, value):
        self.writes.append(value)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_daq(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    mod = types.ModuleType("nidaqmx")
    const = types.ModuleType("nidaqmx.constants")
    const.AcquisitionType = types.SimpleNamespace(FINITE="finite")
    const.TerminalConfiguration = types.SimpleNamespace(
        DEFAULT="default", RSE="rse", NRSE="nrse", DIFF="diff")
    mod.Task = _Task
    mod.constants = const
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", const)
    monkeypatch.setattr(_Task, "fail_on_add", False)
    _Task.instances = []
    return mod


def _hw(dev="Dev1") -> Hardware:
    hw = Hardware()
    for name in ("ao_x", "ao_y", "ai_hall_x", "ai_hall_y", "ai_temp1", "ai_temp2",
                 "di_water", "do_enable"):
        _, _, ch = getattr(hw, name).partition("/")
        setattr(hw, name, f"{dev}/{ch}")
    return hw


# ------------------------------------------------------------------------ tests

def test_devices_from_channels():
    assert daq_devices(Hardware()) == ["Dev1"]
    hw = Hardware()
    hw.ai_temp1 = "/Dev2/ai0"          # a leading slash is legal DAQmx
    assert daq_devices(hw) == ["Dev1", "Dev2"]


def test_second_open_same_card_is_refused(fake_daq):
    a = NidaqVectorMagnet(_hw())
    a.open()
    try:
        n_tasks = len(_Task.instances)
        b = NidaqVectorMagnet(_hw())
        with pytest.raises(HardwareBusy) as exc:
            b.open()
        assert "mag2dcal" in str(exc.value)
        assert "DEV1" in str(exc.value)
        # The refused backend created NO task, so it sent nothing to the card.
        assert len(_Task.instances) == n_tasks
    finally:
        a.close()


def test_same_card_spelled_differently_conflicts(fake_daq):
    a = NidaqVectorMagnet(_hw("Dev1"))
    a.open()
    try:
        with pytest.raises(HardwareBusy):
            NidaqVectorMagnet(_hw("dev1")).open()
        # a different card is a different instrument
        c = NidaqVectorMagnet(_hw("Dev2"))
        c.open()
        c.close()
    finally:
        a.close()


def test_close_releases(fake_daq):
    a = NidaqVectorMagnet(_hw())
    a.open()
    assert [h["normalized"] for h in hwlock.held()] == ["DEV1"]
    a.close()
    assert hwlock.held() == []
    b = NidaqVectorMagnet(_hw())
    b.open()                            # must succeed now
    b.close()


def test_failing_open_releases(fake_daq, monkeypatch):
    monkeypatch.setattr(_Task, "fail_on_add", True)
    a = NidaqVectorMagnet(_hw())
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []
    monkeypatch.setattr(_Task, "fail_on_add", False)
    b = NidaqVectorMagnet(_hw())
    b.open()
    b.close()


def test_missing_nidaqmx_releases(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setitem(sys.modules, "nidaqmx", None)   # import -> ImportError
    with pytest.raises(ImportError):
        NidaqVectorMagnet(_hw()).open()
    assert hwlock.held() == []


def test_sim_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    cfg = Config()
    cfg.interlock.water_bypass = True
    cfg.calibration.load_newest_on_start = False
    ctrl, _ = build_sim_system(cfg)
    ctrl.start(run_thread=False)
    try:
        assert hwlock.held() == []
    finally:
        ctrl.shutdown()
    assert hwlock.held() == []


def test_controller_busy_sends_nothing(fake_daq):
    """The service path: a controller whose backend is refused never counts
    itself open, so shutdown() does not ramp a magnet it does not own."""
    from mag2dcal.controller import Controller
    owner = NidaqVectorMagnet(_hw())
    owner.open()
    try:
        cfg = Config()
        cfg.calibration.load_newest_on_start = False
        ctrl = Controller(NidaqVectorMagnet(_hw()), cfg)
        n_tasks = len(_Task.instances)
        with pytest.raises(HardwareBusy):
            ctrl.start(run_thread=False)
        ctrl.shutdown()                  # must be a no-op
        assert len(_Task.instances) == n_tasks
        assert not any(t.writes for t in _Task.instances[n_tasks:])
    finally:
        owner.close()
