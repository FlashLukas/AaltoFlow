"""The Analog Discovery backend (backends/dwf.py) against a fake dwf library
(fake_dwf.py): no device, no WaveForms. Each rule has a test that fails
without it:

  * one device, three parts: scope + generator + supplies share ONE handle,
    opened once, closed by the last user; the address is claimed (hwlock);
  * read-only start: opening and adopting send no Set / Configure at all;
  * the scope's conventions: V/div = range / 8, offset = -(dwf offset),
    time/div = buffer / rate / 10, the range snaps to 5 / 50 V;
  * records: armed when the brain first asks, data read when Done;
  * the generator: dwf amplitude is the PEAK (1 Vpp asked = 0.5 sent, read
    back as 1 Vpp); duty / symmetry share one node; output = configure start;
  * trigger sources: T1 = trigsrcExternal1, W1 = the generator;
  * supplies found by NAME, master switch on with the first enable, and the
    module's limits; a restart (keep_outputs) sets "keep running on close".
"""

import time

import pytest

import scope.backends.dwf as D
from scope import hwlock
from scope.config import Config
from scope.generator.brain import Generator
from scope.generator.config import GenConfig
from scope.hwlock import HardwareBusy
from scope.scope import Scope

from fake_dwf import FakeDwf


@pytest.fixture
def fake(monkeypatch):
    f = FakeDwf()
    monkeypatch.setattr(D, "_load_library", lambda: f)
    return f


def build(fake, device=""):
    dev = D.DwfDevice(device)
    scope = Scope(D.DwfScope(dev), Config(), gen=Generator(D.DwfWaveGen(dev), GenConfig()))
    events = []
    scope._on_event = lambda level, msg: events.append((level, msg))
    return scope, dev, events


def test_start_is_read_only_and_shares_one_handle(fake):
    scope, dev, events = build(fake)
    scope.start(run=False)
    try:
        # nothing changed at start -- except the driver's mode (dynamic
        # auto-configure: how later changes are applied, not a setting)
        assert fake.sent() == ["FDwfDeviceAutoConfigureSet"], fake.sent()
        assert [c[0] for c in fake.calls].count("FDwfDeviceOpen") == 1
        held = hwlock.held()
        assert len(held) == 1 and "FAKE0001" in str(held[0])
        st = scope.status()
        assert st["model"] == "Analog Discovery 2" and st["generator_channels"] == 2
        assert st["supply_vplus_on"] is False and "USB Monitor Voltage V" in st["monitors"]
        assert st["ch1_vdiv_V"] == pytest.approx(5.0 / 8) and st["ch1_coupling"] == "dc"
    finally:
        scope.shutdown()
    assert fake.handle == 0 and hwlock.held() == []


def test_a_second_service_is_refused(fake):
    a = D.DwfDevice()
    a.open()
    try:
        with pytest.raises(HardwareBusy):
            D.DwfDevice("FAKE0001").open()
    finally:
        a.close()


def test_device_choice_by_serial_or_index(fake):
    fake.devices = [["SN:AAA", "Analog Discovery 2", True], ["SN:BBB", "Analog Discovery 3", False]]
    d = D.DwfDevice()
    d.open(); assert d.name == "Analog Discovery 3"; d.close()        # first FREE one
    d = D.DwfDevice("#0")
    d.open(); assert d.serial == "SN:AAA"; d.close()
    with pytest.raises(D.DwfError):
        D.DwfDevice("ZZZ").open()


def test_scope_settings_map_to_dwf(fake):
    dev = D.DwfDevice()
    sc = D.DwfScope(dev)
    sc.open()
    try:
        sc.set_channel("ch1", vdiv_V=0.7, offset_V=0.25)
        assert fake.ain["range"][0] == 50.0                # 8 x 0.7 V > 5 V: the 50 V range
        assert fake.ain["offset"][0] == pytest.approx(-0.25)
        sc.set_timebase(tdiv_s=1e-3)
        assert fake.ain["rate"] == pytest.approx(8192 / 10e-3)
        got = sc.read_settings()
        assert got["tdiv_s"] == pytest.approx(1e-3) and got["channels"]["ch1"]["vdiv_V"] == 6.25
        assert got["channels"]["ch1"]["probe"] == 1.0     # no attenuation call: 1
        with pytest.raises(ValueError):
            sc.set_channel("ch2", coupling="ac")
        sc.set_trigger(source="ext1", slope="falling", mode="normal")
        assert fake.ain["trig_src"] == 11 and fake.ain["cond"] == 1 and fake.ain["auto"] == 0
        sc.set_trigger(source="w1", mode="auto")
        assert fake.ain["trig_src"] == 7 and fake.ain["auto"] > 0
        sc.set_trigger(source="ch2")
        assert fake.ain["trig_src"] == 2 and fake.ain["trig_ch"] == 1
        # hysteresis: 0.05 division of the source (lab AD2: noise made the
        # trigger fire on the wrong edge without it)
        assert fake.hysteresis == pytest.approx(0.05 * 5.0 / 8)
    finally:
        sc.close()


def test_records_come_from_the_device(fake):
    scope, dev, events = build(fake)
    scope.start(run=False)
    try:
        fake.aout[0].update(running=True, amp=0.5, freq=1000.0)  # W1 on (looped to CH1)
        scope.set_tdiv(1e-3)
        for _ in range(12):
            scope._next_poll = -1e9
            scope.step()
        assert scope.status()["records"] >= 2
        live = scope.status()["live"]["ch1"]
        assert live["pk2pk"] == pytest.approx(1.0, rel=0.01)
        assert live["frequency"] == pytest.approx(1000.0, rel=1e-3)
    finally:
        scope.shutdown()


def test_generator_maps_amplitude_and_output(fake):
    dev = D.DwfDevice()
    g = D.DwfWaveGen(dev)
    g.open()
    try:
        g.set_amplitude(0, 1.0)
        assert fake.aout[0]["amp"] == pytest.approx(0.5)   # dwf amplitude = peak
        assert g.read_channel(0)["amplitude_Vpp"] == pytest.approx(1.0)
        g.set_waveform(0, "pulse"); g.set_duty(0, 20.0)
        rc = g.read_channel(0)
        assert rc["waveform"] == "pulse" and rc["duty_pct"] == 20.0 and fake.aout[0]["sym"] == 20.0
        g.set_output(0, True)
        assert fake.aout[0]["running"] and fake.aout[0]["enabled"] == 1
        assert g.read_channel(0)["output"] is True
        assert g.envelope("sine")["freq_max_Hz"] == 20e6   # the bandwidth, not 100 MHz
        # LAB AD2: a change while running stopped the output -- it must keep running
        assert fake.autoconfigure == 3                    # dynamic, set at open
        g.set_frequency(0, 2000.0); g.set_amplitude(0, 0.4)
        assert fake.aout[0]["running"] and fake.aout[0]["freq"] == 2000.0
        # an older runtime without dynamic mode: the status is READ and a stopped
        # output started again (Configure 3 "succeeds" but does nothing there)
        fake.autoconfigure = 1
        g.set_frequency(0, 3000.0)
        assert fake.aout[0]["running"] and fake.aout[0]["freq"] == 3000.0
        fake.autoconfigure = 3
        # a phase between two running outputs: a synced start (W2 slaved to W1)
        g.set_output(1, True)
        assert fake.master == (1, 0)
        fake.master = None
        n0, k0 = fake.synced_starts, len(fake.calls)
        g.set_phase(1, 90.0)
        assert fake.master == (1, 0) and fake.aout[0]["running"] and fake.aout[1]["running"]
        # the sequence measured good: W2 slaved, W1 (re)started -- never W2 on its own
        # (cc46ac7 started W2 first: it ran free, -78 deg on the AD2)
        cfg = [c for c in fake.calls[k0:] if c[0] == "FDwfAnalogOutConfigure"]
        assert [c[2] for c in cfg] == [0] and fake.synced_starts == n0 + 1
        fake.master = None
        g.align_phase()
        assert fake.master == (1, 0)
    finally:
        g.close()
    assert fake.aout[0]["running"] is False               # off on close


def test_supplies_by_name_and_keep_on_restart(fake, monkeypatch):
    for keep in (False, True):
        f = FakeDwf()
        monkeypatch.setattr(D, "_load_library", lambda f=f: f)
        scope, dev, events = build(f)
        scope.start(run=False)
        scope.set_supply("vplus", on=True, volts=3.0)
        assert f.io_set[(0, 0)] == 1.0 and f.io_set[(0, 1)] == 3.0 and f.io_master == 1
        st = scope.status()
        assert st["supply_vplus_on"] and st["supply_vplus_V"] == pytest.approx(3.0)
        # the AD2's supplies have NO readback (status range with 0 steps):
        # nothing measured -- not an echo (lab 2026-10-10: V- "read" -0.2057)
        assert st["supply_vplus_meas_V"] is None and st["supply_vminus_meas_V"] is None
        assert st["supply_vplus_meas_A"] is None
        # the SETTABLE range (NodeSetInfo; NodeInfo is the node TYPE: lab
        # 2026-10-10 "allowed 9.88e-324 .. 0 V")
        assert scope.supply_limits("vplus") == (0.5, 5.0)
        assert scope.supply_limits("vminus") == (-5.0, -0.5)
        with pytest.raises(ValueError, match="switch the supply off"):
            scope.set_supply("vplus", volts=0.2)         # the dead band: refused
        with pytest.raises(ValueError, match="switch the supply off"):
            scope.set_supply("vminus", volts=-0.3)
        scope.set_supply("vminus", volts=-7.0)            # beyond: clamped, warned
        assert f.io_set[(1, 1)] == -5.0
        assert any("clamped to -5" in m for _, m in events)
        mon = st["monitors"]
        assert mon["USB Monitor Voltage V"] == pytest.approx(4.756)
        assert mon["USB Monitor Temperature C"] == pytest.approx(39.0)
        assert not any("Supply" in k for k in mon)       # supplies are not monitors
        scope.gen.set_output("w1", True)
        deadline = time.monotonic() + 5
        while not f.aout[0]["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
        scope.shutdown(keep_outputs=keep)
        assert (f.on_close == 0) is keep                    # "keep running" on close
        assert bool(f.io_set[(0, 0)]) is keep               # supply left on only on restart
        assert f.aout[0]["running"] is keep


def test_frequency_rounding_is_not_a_mismatch():
    """Lab AD2: 1000 Hz read back as 1000.0000222 -- w1_settled never came."""
    from scope.generator.brain import _same
    assert _same("frequency_Hz", 1000.0, 1000.0000221897726)
    assert not _same("frequency_Hz", 1000.0, 1000.1)


def test_service_prints_events(capsys):
    """Start-up events reach the launcher log (a GUI connecting later never
    sees them on PUB) -- ASCII only."""
    from scope.net.service import ScopeService
    from scope.sim_system import build_sim_system
    scope, _ = build_sim_system(Config())
    svc = ScopeService(scope, host="127.0.0.1", cmd_port=17648, pub_port=17649)
    svc._event("info", "supplies found: V+ off µ")
    assert "[info] supplies found: V+ off ?" in capsys.readouterr().out


def test_a_quantised_level_is_not_a_change():
    """Lab AD2: trigger level 0 read back as -0.0017 V -> "changed at the
    scope: trigger_level_V" and "asked 0, the scope set -0.0017"."""
    from scope.scope import _differs
    actual = {"trigger_source": "ch1", "ch1_vdiv_V": 0.625}
    assert not _differs("trigger_level_V", 0.0, -0.00170385, actual)
    assert _differs("trigger_level_V", 0.0, 0.2, actual)
    assert not _differs("ch1_offset_V", 0.1, 0.1031, actual)
    assert _differs("tdiv_s", 0.02, 0.01, actual)
