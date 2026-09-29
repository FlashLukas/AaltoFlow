"""One USB-6001, one service: the real backend claims the card by its SERIAL
number (and by its DAQmx name) before the first task exists.

All offline: nidaqmx is a fake put into sys.modules, the lock folder is a temp
dir (conftest.py sets AALTOFLOW_LOCK_DIR).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from usb6001 import hwlock
from usb6001.backends.base import Layout
from usb6001.backends.nidaq import NidaqUsb6001, serial_address
from usb6001.config import Config
from usb6001.daq import Daq, layout_from
from usb6001.sim_system import demo_config

ROOT = Path(__file__).resolve().parents[1]


class _Chans:
    def __init__(self, task, kind):
        self.task, self.kind = task, kind

    def _add(self, name, **kw):
        self.task.state["created"].append((self.kind, name, kw))
        self.task.channels.append(name)

    add_ai_voltage_chan = add_ao_voltage_chan = add_di_chan = add_do_chan = _add


class _Timing:
    def __init__(self, task):
        self.task = task

    def cfg_samp_clk_timing(self, rate, sample_mode=None, samps_per_chan=None):
        self.task.n = samps_per_chan


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    """A nidaqmx with two aliases for ONE physical card (same serial) and a
    second card. Records every task, channel and write."""
    state = {"created": [], "writes": [], "tasks": [], "fail_task": None,
             "devices": {"Dev1": 0x1F2E3D4, "Dev7": 0x1F2E3D4, "Dev2": 0x55AA}}

    class Task:
        def __init__(self, name=""):
            if state["fail_task"] and name.startswith(state["fail_task"]):
                raise OSError(f"DAQmx error creating {name} (fake)")
            self.name, self.state, self.channels, self.n = name, state, [], 2
            self.ai_channels = _Chans(self, "ai")
            self.ao_channels = _Chans(self, "ao")
            self.di_channels = _Chans(self, "di")
            self.do_channels = _Chans(self, "do")
            self.timing = _Timing(self)
            self.closed = False
            state["tasks"].append(self)

        def read(self, number_of_samples_per_channel=None, timeout=None):
            if self.name == "usb6001_ai":
                data = [[0.1 * (k + 1)] * self.n for k in range(len(self.channels))]
                return data[0] if len(data) == 1 else data
            if self.name == "usb6001_di":
                return [True, False][: len(self.channels)] + [False] * (len(self.channels) - 2)
            return False

        def write(self, value):
            state["writes"].append((self.name, value))

        def stop(self):
            pass

        def close(self):
            self.closed = True

    class Device:
        def __init__(self, name):
            if name not in state["devices"]:
                raise OSError(f"no device {name} (fake)")
            self.serial_num = state["devices"][name]
            self.product_type = "USB-6001"

    consts = types.ModuleType("nidaqmx.constants")
    consts.TerminalConfiguration = types.SimpleNamespace(RSE="RSE", NRSE="NRSE", DIFF="DIFF")
    consts.LineGrouping = types.SimpleNamespace(CHAN_PER_LINE="per_line")
    consts.AcquisitionType = types.SimpleNamespace(FINITE="finite")
    system = types.ModuleType("nidaqmx.system")
    system.Device = Device
    mod = types.ModuleType("nidaqmx")
    mod.Task, mod.constants, mod.system = Task, consts, system
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", consts)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", system)
    return state


def _backend(device="Dev1"):
    cfg = Config()
    cfg.hardware.device = device
    return NidaqUsb6001(cfg.hardware)


LAYOUT = layout_from(demo_config())


def test_same_card_under_another_alias_is_refused(fake_nidaqmx):
    a = _backend("Dev1")
    a.open(LAYOUT)
    n_tasks = len(fake_nidaqmx["tasks"])
    b = _backend("Dev7")                      # NI MAX alias of the SAME serial
    with pytest.raises(hwlock.HardwareBusy, match="usb6001"):
        b.open(LAYOUT)
    assert len(fake_nidaqmx["tasks"]) == n_tasks   # the refused one created no task
    a.close()


def test_same_alias_is_refused_and_other_card_is_fine(fake_nidaqmx):
    a = _backend("Dev1")
    a.open(LAYOUT)
    with pytest.raises(hwlock.HardwareBusy):
        _backend("dev1").open(LAYOUT)            # DAQmx names ignore case
    c = _backend("Dev2")                          # another card
    c.open(LAYOUT)
    assert len(hwlock.held()) == 4                # two claims per card: name + serial
    a.close(); c.close()
    assert hwlock.held() == []


def test_open_creates_the_configured_tasks_and_writes_nothing(fake_nidaqmx):
    a = _backend()
    a.open(LAYOUT)
    kinds = [(k, n) for k, n, _ in fake_nidaqmx["created"]]
    assert ("ai", "Dev1/ai0") in kinds and ("ai", "Dev1/ai4") not in kinds
    assert ("ao", "Dev1/ao0") in kinds and ("ao", "Dev1/ao1") in kinds
    assert ("di", "Dev1/port0/line0") in kinds and ("di", "Dev1/port2/line0") in kinds
    assert ("do", "Dev1/port0/line4") in kinds and ("do", "Dev1/port1/line0") not in kinds
    assert fake_nidaqmx["writes"] == []           # adopt on start
    assert "SN 1F2E3D4" in a.idn()
    # reads come back per channel / per line, in layout order
    assert a.read_ai(10, 1000.0) == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert a.read_di()[0] is True
    a.write_ao(1, 2.5)
    a.write_do(5, True)
    assert fake_nidaqmx["writes"] == [("usb6001_ao1", 2.5), ("usb6001_do_5", True)]
    a.close()
    assert fake_nidaqmx["writes"] == [("usb6001_ao1", 2.5), ("usb6001_do_5", True)]
    assert all(t.closed for t in fake_nidaqmx["tasks"])


def test_single_channel_read_is_flat_in_nidaqmx(fake_nidaqmx):
    a = _backend()
    a.open(Layout(ai=(3,), ai_terminal=("RSE",)))
    assert a.read_ai(5, 100.0) == pytest.approx([0.1])
    a.close()


@pytest.mark.parametrize("fail", ["usb6001_ai", "usb6001_do_"])
def test_failing_open_releases_the_claims(fake_nidaqmx, fail):
    fake_nidaqmx["fail_task"] = fail
    with pytest.raises(OSError):
        _backend().open(LAYOUT)
    assert hwlock.held() == []
    fake_nidaqmx["fail_task"] = None
    b = _backend()
    b.open(LAYOUT)
    b.close()


def test_unknown_device_releases_the_name_claim(fake_nidaqmx):
    with pytest.raises(OSError):
        _backend("Dev9").open(LAYOUT)
    assert hwlock.held() == []


def test_busy_start_writes_nothing_to_the_other_services_card(fake_nidaqmx):
    cfg = demo_config()
    cfg.dio.lines[4].initial = "high"
    cfg.dio.lines[5].safe_state = "low"
    owner = _backend("Dev1")
    owner.open(layout_from(cfg))
    daq = Daq(_backend("Dev7"), cfg)
    with pytest.raises(hwlock.HardwareBusy):
        daq.start(poll=False)
    daq.shutdown()                                # the crash path of the service
    assert fake_nidaqmx["writes"] == []           # no initial, no safe state
    owner.close()


def test_serial_address_spelling():
    assert serial_address(0x1F2E3D4) == "NI-DAQ-SN:1F2E3D4"
    assert serial_address("1f2e3d4") == "NI-DAQ-SN:1F2E3D4"


def test_sim_claims_nothing():
    from usb6001.sim_system import build_sim_system
    daq, _ = build_sim_system(Config())
    daq.start(poll=False)
    assert hwlock.held() == []
    daq.shutdown()


def test_run_service_exits_4_when_the_card_is_taken(fake_nidaqmx, tmp_path):
    spec = importlib.util.spec_from_file_location("usb6001_run_service",
                                                  ROOT / "scripts" / "run_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    owner = _backend("Dev1")
    owner.open(LAYOUT)
    try:
        code = mod.main(["--real", "--device", "Dev7", "--cmd-port", "17760",
                         "--pub-port", "17761", "--config", str(tmp_path / "none.ini")])
    finally:
        owner.close()
    assert code == mod.EXIT_HARDWARE_BUSY == 4


def test_the_module_copy_of_hwlock_is_the_master():
    master = (Path(__file__).resolve().parents[4] / "suite-common" / "src"
              / "suite_common" / "hwlock.py")
    if not master.exists():
        pytest.skip("suite-common not next to this module (installed on its own)")
    assert Path(hwlock.__file__).read_bytes() == master.read_bytes()
