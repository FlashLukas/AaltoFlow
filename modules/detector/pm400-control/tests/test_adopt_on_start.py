"""Adopt-on-start (Lukas's rule, 2026-09-27): starting the service READS the
console and changes nothing on it.

Two layers are checked:
  * the brain, against the simulator wrapped in a recorder that FAILS on any
    state-changing call during start-up;
  * the real TLPMX backend, against a fake TLPMX_64.dll that records every
    library function called -- so a stray TLPMX_set... in open() or in the
    brain's start-up is caught without a console on the desk.
The simulator is first put in a NON-default state ("someone set it by hand"),
otherwise adopting and pushing the defaults would look the same.
"""

import ctypes as C
import math

import pytest

from pm400.backends import tlpmx
from pm400.config import Config
from pm400.meter import Pm400Meter
from pm400.net.describe import build_manifest
from pm400.sim_system import build_sim_system

# Backend methods that change the console's state.
WRITES = {"set_wavelength", "set_auto_range", "set_range", "set_energy_range",
          "set_avg_time", "start_zero", "cancel_zero"}


class StrictBackend:
    """Forwards to the real (simulated) backend; while `strict`, any write fails."""

    def __init__(self, inner):
        self._inner = inner
        self.strict = True
        self.writes = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in WRITES:
            def guarded(*args, **kw):
                if self.strict:
                    raise AssertionError(f"state-changing call at start-up: {name}{args}")
                self.writes.append(name)
                return attr(*args, **kw)
            return guarded
        return attr


def _system(head="photodiode", **panel):
    cfg = Config()                        # defaults: 800 nm, auto range, 0.1 s
    cfg.sim.head = head
    meter, sim = build_sim_system(cfg, realtime=False, seed=1, zero_time_s=0.0)
    sim.front_panel(**panel)
    strict = StrictBackend(sim)
    meter.backend = strict
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    return meter, sim, strict, cfg, events


def test_start_issues_no_writes_and_adopts_a_manual_power_setup():
    meter, sim, strict, cfg, _ = _system(
        wavelength_nm=532.0, auto_range=False, range_=1e-2, avg_time_s=0.25)
    meter.start(poll=False)
    for _ in range(3):                    # the poll thread's work: still read-only
        meter.poll_once()
    meter.check_head()
    s = meter.status()
    assert s.wavelength_nm == 532.0 and s.wavelength_set_nm == 532.0
    assert s.auto_range is False
    assert s.range == pytest.approx(1e-2) and s.range_set == pytest.approx(1e-2)
    assert s.avg_time_s == pytest.approx(0.25)
    # the config mirrors the console, so Save config stores what is in use
    assert cfg.sensor.wavelength_nm == 532.0 and cfg.sensor.auto_range is False
    assert cfg.sensor.range_W == pytest.approx(1e-2)
    assert cfg.sensor.avg_time_s == pytest.approx(0.25)
    # and the console itself was left exactly as found
    assert sim.get_wavelength() == 532.0 and sim.get_auto_range() is False
    assert strict.writes == []
    meter.shutdown()


def test_describe_follows_the_adopted_state():
    # console on MANUAL range -> describe offers range as a CONTROL
    meter, *_ = _system(auto_range=False, range_=1e-3)
    meter.start(poll=False)
    rng = {p["id"]: p for p in build_manifest(meter)["parameters"]}["range"]
    assert rng["kind"] == "control"
    meter.shutdown()
    # console on AUTO range -> an indicator
    meter, *_ = _system(auto_range=True)
    meter.start(poll=False)
    rng = {p["id"]: p for p in build_manifest(meter)["parameters"]}["range"]
    assert rng["kind"] == "indicator"
    meter.shutdown()


def test_pyro_head_energy_range_is_adopted_without_writes():
    meter, sim, strict, cfg, _ = _system(head="pyro", wavelength_nm=1550.0, range_=1.5e-2)
    meter.start(poll=False)
    meter.check_head()
    s = meter.status()
    assert s.quantity == "energy"
    assert s.wavelength_nm == 1550.0 and s.range == pytest.approx(1.5e-2)
    assert cfg.sensor.range_J == pytest.approx(1.5e-2)
    assert strict.writes == []
    meter.shutdown()


def test_setting_outside_our_limits_is_kept_and_reported_not_corrected():
    # the console was left averaging 3 s; our envelope says 1 s max
    meter, sim, strict, cfg, events = _system(avg_time_s=3.0)
    meter.start(poll=False)
    assert meter.status().avg_time_s == pytest.approx(3.0)
    assert sim.get_avg_time() == pytest.approx(3.0)
    assert any(lvl == "warn" and "averaging time" in m for lvl, m in events)
    meter.shutdown()


def test_explicit_settings_are_still_sent_after_start():
    meter, sim, strict, cfg, _ = _system(wavelength_nm=532.0)
    meter.start(poll=False)
    strict.strict = False                 # start-up is over; the USER sets things now
    meter.set_wavelength(700.0)
    assert sim.get_wavelength() == 700.0 and "set_wavelength" in strict.writes
    meter.shutdown()


# ---- the real backend against a fake TLPMX_64.dll ----------------------------

def _obj(arg):
    """The ctypes object behind a byref() argument."""
    return getattr(arg, "_obj", arg)


class FakeTLPMX:
    """A PM400 with an S121C head, left at 532 nm / manual 10 mW / 0.25 s.
    Records every library function called; a state-changing one FAILS."""

    ALLOWED_WRITES = {"TLPMX_init",            # opening the session (reset arg checked)
                      "TLPMX_setTimeoutValue"}  # the driver's USB timeout on THIS PC

    def __init__(self):
        self.calls = []
        self.init_args = None

    def __getattr__(self, name):
        if not name.startswith("TLPMX_"):
            raise AttributeError(name)

        def fn(*args):
            self.calls.append(name)
            if (name.startswith("TLPMX_set") or "DarkAdjust" in name and
                    not name.startswith("TLPMX_get")) and name not in self.ALLOWED_WRITES:
                raise AssertionError(f"state-changing TLPMX call at start-up: {name}")
            return self._answer(name, args)
        return fn

    def _answer(self, name, a):
        if name == "TLPMX_init":
            self.init_args = a
            _obj(a[3]).value = 1
        elif name == "TLPMX_identificationQuery":
            for buf, text in zip(a[1:5], (b"Thorlabs", b"PM400", b"P5000000", b"1.0")):
                buf.value = text
        elif name == "TLPMX_getSensorInfo":
            a[1].value, a[2].value = b"S121C", b"123"
            _obj(a[4]).value = tlpmx.SENSOR_TYPE_PD_SINGLE
            _obj(a[6]).value = tlpmx.SENS_FLAG_IS_POWER | tlpmx.SENS_FLAG_IS_WAVEL_SET
        elif name in ("TLPMX_getWavelength", "TLPMX_getPowerRange", "TLPMX_getAvgTime"):
            table = {"TLPMX_getWavelength": (532.0, 400.0, 1100.0),
                     "TLPMX_getPowerRange": (1e-2, 1e-6, 0.5),
                     "TLPMX_getAvgTime": (0.25, 0.001, 10.0)}
            _obj(a[2]).value = table[name][a[1]]
        elif name == "TLPMX_getPowerAutorange":
            _obj(a[1]).value = 0
        elif name == "TLPMX_measPower":
            _obj(a[1]).value = 1.23e-3
        elif name == "TLPMX_getDarkOffset":
            _obj(a[1]).value = 1e-9
        return 0


def test_real_backend_start_is_read_only(monkeypatch):
    fake = FakeTLPMX()
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": fake)
    backend = tlpmx.TLPMXConsole(resource="USB0::0x1313::0x807D::P5000000::INSTR")
    meter = Pm400Meter(backend, Config())
    meter.start(poll=False)
    meter.poll_once()
    meter.check_head()
    # TLPMX_init(resource, IDQuery=1, resetDevice=0, &vi): a reset would wipe
    # the console's wavelength / range, so it must stay OFF.
    assert fake.init_args[2] == 0
    s = meter.status()
    assert s.wavelength_nm == 532.0 and s.auto_range is False
    assert s.range == pytest.approx(1e-2) and s.avg_time_s == pytest.approx(0.25)
    assert s.value == pytest.approx(1.23e-3)
    written = [c for c in fake.calls if c.startswith("TLPMX_set")]
    assert set(written) <= {"TLPMX_setTimeoutValue"}
    meter.shutdown()


def test_real_backend_timeout_follows_an_adopted_long_average(monkeypatch):
    # A console left at 4 s averaging needs > 4 s per measPower; the session
    # timeout (a driver setting on this PC, not a console setting) must follow.
    fake = FakeTLPMX()
    orig = fake._answer

    def answer(name, a):
        r = orig(name, a)
        if name == "TLPMX_getAvgTime" and a[1] == 0:
            _obj(a[2]).value = 4.0
        return r
    fake._answer = answer
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": fake)
    backend = tlpmx.TLPMXConsole(resource="X", timeout_ms=5000)
    meter = Pm400Meter(backend, Config())
    meter.start(poll=False)
    assert backend.timeout_ms >= 6000
    assert math.isclose(meter.status().avg_time_s, 4.0)
    meter.shutdown()
