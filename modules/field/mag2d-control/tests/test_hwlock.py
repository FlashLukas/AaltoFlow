"""One DAQ card, one service: the real backend claims its device (hwlock).

mag2d and mag2dcal drive the same coils through the same card, so the NI
backend claims the DAQmx DEVICE NAME ("Dev1") before it creates a task. These
tests run offline: a minimal fake `nidaqmx` module stands in for the NI
driver, and AALTOFLOW_LOCK_DIR points the lock files at pytest's tmp_path, so
nothing here touches the real lock folder or a running service.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from mag2d import hwlock
from mag2d.backends.nidaq import NidaqVectorMagnet, daq_devices
from mag2d.backends.sim import SimVectorMagnet
from mag2d.config import Config, Hardware
from mag2d.controller import Controller


# ---------------------------------------------------------------- fake nidaqmx

class _Chans:
    def __init__(self, task):
        self.task = task

    def _add(self, name, **_kw):
        if self.task.fake.fail_on and self.task.fake.fail_on in name:
            raise RuntimeError(f"fake DAQmx: {name} does not exist")
        self.task.channels.append(name)

    add_ao_voltage_chan = add_ai_voltage_chan = add_di_chan = add_do_chan = _add


class _Task:
    def __init__(self, fake, name=""):
        self.fake, self.name, self.channels = fake, name, []
        self.ao_channels = self.ai_channels = _Chans(self)
        self.di_channels = self.do_channels = _Chans(self)
        self.timing = types.SimpleNamespace(cfg_samp_clk_timing=lambda **kw: None)
        fake.created.append(name)

    def read(self, number_of_samples_per_channel=None, timeout=None):
        if self.name == "mag2d_ai":
            n = number_of_samples_per_channel or 2
            return [[2.4915] * n, [2.4973] * n, [0.002] * n, [0.002] * n]
        if self.name == "mag2d_ao_readback":
            return [0.0, 0.0]
        return True                                   # water flowing / enable line

    def write(self, value):
        self.fake.writes.append((self.name, value))

    def close(self):
        pass


def _fake_nidaqmx(fail_on: str | None = None):
    fake = types.ModuleType("nidaqmx")
    fake.fail_on, fake.created, fake.writes = fail_on, [], []
    fake.Task = lambda name="": _Task(fake, name)
    consts = types.ModuleType("nidaqmx.constants")
    consts.AcquisitionType = types.SimpleNamespace(FINITE="finite")
    consts.TerminalConfiguration = types.SimpleNamespace(
        DEFAULT="default", RSE="rse", NRSE="nrse", DIFF="diff")
    fake.constants = consts
    return fake


@pytest.fixture
def daq(monkeypatch, tmp_path):
    """Install a fake nidaqmx and a private lock folder; return the fake."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    fake = _fake_nidaqmx()
    monkeypatch.setitem(sys.modules, "nidaqmx", fake)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", fake.constants)
    return fake


def _hw(dev: str = "Dev1") -> Hardware:
    hw = Hardware()
    for f in ("ao_x", "ao_y", "ai_hall_x", "ai_hall_y", "ai_temp1", "ai_temp2",
              "di_water", "do_enable"):
        setattr(hw, f, getattr(hw, f).replace("Dev1", dev))
    return hw


# ---------------------------------------------------------------- tests

def test_copy_is_identical_to_master():
    """Modules carry a byte-identical copy (check_modules.py compares too)."""
    here = os.path.dirname(os.path.abspath(hwlock.__file__))
    master = os.path.join(here, "..", "..", "..", "..", "..", "suite-common",
                          "src", "suite_common", "hwlock.py")
    if not os.path.isfile(master):
        pytest.skip("suite-common not next to this module (installed alone)")
    with open(master, "rb") as a, open(hwlock.__file__, "rb") as b:
        assert a.read() == b.read()


def test_devices_from_channel_names():
    assert daq_devices(Hardware()) == ["Dev1"]
    hw = Hardware()
    hw.ai_temp1, hw.di_water = "Dev2/ai0", "/dev1/port0/line1"
    assert [d.upper() for d in daq_devices(hw)] == ["DEV1", "DEV2"]


def test_second_open_on_same_card_is_refused(daq):
    a = NidaqVectorMagnet(_hw())
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["mag2d"]
    b = NidaqVectorMagnet(_hw())
    n_tasks = len(daq.created)
    with pytest.raises(hwlock.HardwareBusy) as ei:
        b.open()
    assert "mag2d" in str(ei.value) and "DEV1" in str(ei.value)
    assert len(daq.created) == n_tasks, "a refused open must not create a DAQmx task"
    a.close()


def test_same_card_spelled_differently_conflicts(daq):
    a = NidaqVectorMagnet(_hw("Dev1"))
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        NidaqVectorMagnet(_hw("dev1")).open()
    # and a claim from ANOTHER module (mag2dcal) on the bare name is refused too
    with pytest.raises(hwlock.HardwareBusy):
        hwlock.claim("DEV1", "mag2dcal", wait_s=0.0)
    a.close()


def test_other_module_holding_the_card_blocks_mag2d(daq):
    other = hwlock.claim("Dev1", "mag2dcal", wait_s=0.0)
    with pytest.raises(hwlock.HardwareBusy) as ei:
        NidaqVectorMagnet(_hw()).open()
    assert "mag2dcal" in str(ei.value)
    assert daq.created == [] and daq.writes == []
    other.release()


def test_close_releases(daq):
    a = NidaqVectorMagnet(_hw())
    a.open()
    a.close()
    assert hwlock.held() == []
    b = NidaqVectorMagnet(_hw())
    b.open()                                          # free again
    b.close()


def test_failing_open_releases(daq):
    daq.fail_on = "port0/line1"                       # the water DI task fails
    a = NidaqVectorMagnet(_hw())
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []
    daq.fail_on = None
    b = NidaqVectorMagnet(_hw())
    b.open()
    b.close()


def test_partial_claim_is_rolled_back(daq):
    """Two cards, the second busy: the first must not stay claimed."""
    hw = _hw()
    hw.ai_temp1 = "Dev2/ai0"
    other = hwlock.claim("Dev2", "someone", wait_s=0.0)
    with pytest.raises(hwlock.HardwareBusy):
        NidaqVectorMagnet(hw).open()
    assert [h["module"] for h in hwlock.held()] == ["someone"]
    other.release()


def test_busy_controller_start_writes_nothing(daq):
    """The refused service must not ramp / zero a magnet it does not own."""
    other = hwlock.claim("Dev1", "mag2dcal", wait_s=0.0)
    ctrl = Controller(NidaqVectorMagnet(_hw()), Config())
    with pytest.raises(hwlock.HardwareBusy):
        ctrl.start(run_thread=False)
    ctrl.shutdown()                                   # never opened -> a no-op
    assert daq.writes == []
    other.release()


def test_sim_backend_claims_nothing(daq):
    ctrl = Controller(SimVectorMagnet(), Config())
    ctrl.start(run_thread=False)
    assert hwlock.held() == []
    ctrl.shutdown()
    assert hwlock.held() == []


_CHILD_STUB = """import sys, types
constants = types.ModuleType("nidaqmx.constants")
constants.AcquisitionType = types.SimpleNamespace(FINITE=1)
constants.TerminalConfiguration = types.SimpleNamespace(DEFAULT=1, RSE=2, NRSE=3, DIFF=4)
sys.modules["nidaqmx.constants"] = constants


class Task:
    def __init__(self, *a, **k):
        raise AssertionError("a task was created on a busy card")
"""


def test_run_service_prints_one_line_and_exits_4(daq, tmp_path):
    """Real process: the card is held here, `run_service.py --real` is refused.

    The child gets a stub nidaqmx on PYTHONPATH, so the refusal comes from the
    claim and not from a missing import. It inherits AALTOFLOW_LOCK_DIR.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    # A stand-in `nidaqmx` for the child: enough for the backend's imports,
    # and any task creation fails loudly -- a busy card must see no task.
    (shim / "nidaqmx.py").write_text(_CHILD_STUB, encoding="utf-8")
    held = hwlock.claim("Dev1", "mag2dcal", wait_s=0.0)
    try:
        root = os.path.join(os.path.dirname(__file__), "..")
        env = dict(os.environ, PYTHONPATH=str(shim), PYTHONIOENCODING="ascii")
        p = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "run_service.py"), "--real",
             "--cmd-port", "15990", "--pub-port", "15991"],
            capture_output=True, text=True, env=env, timeout=60)
    finally:
        held.release()
    assert p.returncode == 4, (p.stdout, p.stderr)
    err = p.stderr.strip().splitlines()
    assert len(err) == 1 and "Traceback" not in p.stderr
    assert "DEV1" in err[0] and "mag2dcal" in err[0]
