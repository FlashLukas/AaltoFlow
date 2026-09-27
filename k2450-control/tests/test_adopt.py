"""Start-up ADOPTS the instrument's state and writes nothing (Lukas, 2026-09-27:
"all modules should read the instrument state on startup, not to change
anything").

Two kinds of proof:
  * the simulator is put in a NON-default state first (as if someone had used
    the front panel, or a previous session left it sourcing), and after start
    the status must show exactly that state while the sim's write log is empty;
  * the REAL backend is run against a fake VISA instrument that refuses every
    write except *CLS, so a stray :OUTP OFF or :ROUT:TERM at open() fails loudly.
"""

import sys
import types

import pytest

from k2450.backends.scpi_2450 import VisaK2450
from k2450.config import Config
from k2450.net.describe import build_manifest
from k2450.sim_system import build_sim_system
from k2450.smu import SourceMeter


def _started(preset: dict, **cfg_changes):
    cfg = Config()
    cfg.sim.noise_ppm = 0.0
    for k, v in cfg_changes.items():
        grp, field = k.split("__")
        setattr(getattr(cfg, grp), field, v)
    smu, sim = build_sim_system(cfg, realtime=False, seed=0)
    sim.preset(**preset)
    events = []
    smu._on_event = lambda lvl, msg: events.append((lvl, msg))
    smu.events = events
    smu.start(poll=False)
    return smu, sim


# A plausible "someone left it running" state: sourcing 1.2 mA into the 1 kohm
# pretend resistor, 5 V compliance, output ON, 2 NPLC, 4-wire, rear terminals,
# fixed 10 mA source range -- every one of them different from the config defaults.
BUSY = dict(function="current", level={"current": 1.2e-3, "voltage": 3.3},
            limit={"current": 5.0, "voltage": 2e-3}, output=True, nplc=2.0,
            four_wire=True, terminals="rear", src_auto=False,
            src_range={"current": 1e-2})


def test_start_writes_nothing_and_adopts_a_running_instrument():
    smu, sim = _started(BUSY)
    try:
        assert sim.writes == [], f"start-up wrote to the instrument: {sim.writes}"
        st = smu.status()
        assert st.output is True and sim.get_output() is True   # left ON, as found
        assert st.source_function == "current"
        assert st.source_current_set_A == pytest.approx(1.2e-3)
        assert st.source_current_set_uA == pytest.approx(1200.0)
        assert st.source_voltage_set_V == pytest.approx(3.3)     # inactive pair too
        assert st.voltage_limit_V == pytest.approx(5.0)
        assert st.current_limit_A == pytest.approx(2e-3)
        assert st.nplc == 2.0 and st.four_wire is True
        assert st.source_auto_range is False and st.source_range == pytest.approx(1e-2)
        assert smu.cfg.hardware.terminals == "rear"
        assert st.hw_error == ""
        # and the readings flow at once: 1.2 mA x 1 kohm (4-wire: no leads)
        smu.poll_once()
        assert smu.status().voltage_V == pytest.approx(1.2, rel=1e-3)
        assert sim.writes == []                                 # polling writes nothing
        assert any("adopted" in m and "output ON" in m for _, m in smu.events)
    finally:
        smu.shutdown()


def test_config_values_are_defaults_not_pushed():
    """A .ini asking for 5 V / voltage mode does not reach a 2450 that is
    sourcing current -- only an explicit set does."""
    smu, sim = _started(BUSY, source__function="voltage", source__voltage_V=5.0,
                        measure__nplc=0.1)
    try:
        assert sim.writes == []
        assert sim._fn == "current" and sim._nplc["voltage"] == 2.0
        assert smu.status().source_function == "current"
        smu.set_nplc(0.5)                                       # explicit: written
        assert ("nplc", "voltage", 0.5) in sim.writes
    finally:
        smu.shutdown()


def test_describe_follows_the_adopted_state():
    smu, _ = _started(BUSY)
    try:
        by = {p["id"]: p for p in build_manifest(smu)["parameters"]}
        # sourcing current: the current level is the control, the voltage level is stored
        assert by["source_current"]["kind"] == "control"
    finally:
        smu.shutdown()


def test_outside_the_envelope_is_reported_not_changed():
    smu, sim = _started(dict(function="voltage", level={"voltage": 12.0},
                             limit={"voltage": 1e-3}, output=True),
                        limits__voltage_max_V=5.0)
    try:
        assert sim.writes == [] and sim._level["voltage"] == 12.0
        assert smu.status().source_voltage_set_V == 12.0
        assert any(lvl == "warn" and "outside your envelope" in m for lvl, m in smu.events)
        smu.set_voltage(12.0)                                   # the next change IS clamped
        assert sim._level["voltage"] == 5.0
    finally:
        smu.shutdown()


def test_wrong_measure_function_pauses_readings_until_reselected():
    smu, sim = _started(dict(function="voltage", level={"voltage": 1.0},
                             output=True, sense="resistance"))
    try:
        assert sim.writes == []
        st = smu.status()
        assert "resistance" in st.hw_error
        smu.poll_once()
        assert smu.status().readings == 0                       # nothing mislabelled
        with pytest.raises(ValueError):
            smu.acquire()
        smu.set_source_function("voltage")                      # explicit: fixes it
        assert sim._sense == "current"
        assert smu.status().hw_error == ""
    finally:
        smu.shutdown()


def test_unreadable_state_writes_nothing_and_says_so():
    cfg = Config()
    smu, sim = build_sim_system(cfg, realtime=False, seed=0)
    sim.preset(output=True)

    def broken():
        raise RuntimeError("query timed out")
    sim.read_state = broken
    events = []
    smu._on_event = lambda lvl, msg: events.append((lvl, msg))
    smu.start(poll=False)
    try:
        assert sim.writes == []
        assert smu.status().output is True                      # :OUTP? still answered
        assert "CONFIG defaults" in smu.status().hw_error
        assert any(lvl == "error" for lvl, _ in events)
    finally:
        smu.shutdown()


def test_terminals_change_only_on_request_and_output_off_first():
    smu, sim = _started(BUSY)
    try:
        smu.cfg.hardware.terminals = "front"                    # what set_config does
        smu.apply_config()
        assert ("terminals", "front") in sim.writes
        assert sim.writes.index(("output", False)) < sim.writes.index(("terminals", "front"))
        assert smu.status().output is False
    finally:
        smu.shutdown()


def test_shutdown_still_switches_the_output_off():
    """Shutdown is not part of the adopt rule: an output found ON is still
    switched off when the service stops."""
    smu, sim = _started(BUSY)
    smu.shutdown()
    assert sim.get_output() is False


# ---- the REAL backend against a fake VISA instrument ------------------------------

class FakeInst:
    """Answers the 2450's queries from a table; any write but *CLS fails."""

    ALLOWED = {"*CLS"}

    def __init__(self):
        self.timeout = 0
        self.read_termination = self.write_termination = ""
        self.writes = []
        self.q = {
            "*IDN?": "KEITHLEY INSTRUMENTS,MODEL 2450,FAKE,1.0",
            "*LANG?": "SCPI",
            ":SOUR:FUNC?": "CURR",
            ":SENS:FUNC?": '"VOLT:DC"',
            ":SOUR:VOLT?": "3.3", ":SOUR:CURR?": "0.0012",
            ":SOUR:VOLT:ILIM?": "0.002", ":SOUR:CURR:VLIM?": "5",
            ":SOUR:VOLT:RANG:AUTO?": "1", ":SOUR:CURR:RANG:AUTO?": "0",
            ":SOUR:VOLT:RANG?": "20", ":SOUR:CURR:RANG?": "0.01",
            ":SENS:CURR:RANG:AUTO?": "1", ":SENS:VOLT:RANG:AUTO?": "1",
            ":SENS:CURR:RANG?": "0.0001", ":SENS:VOLT:RANG?": "2",
            ":SENS:CURR:NPLC?": "1", ":SENS:VOLT:NPLC?": "2",
            ":SENS:CURR:RSEN?": "0", ":SENS:VOLT:RSEN?": "1",
            ":OUTP?": "1", ":ROUT:TERM?": "REAR",
            ":SOUR:CURR:READ:BACK?": "1",
            ":SYST:ERR:NEXT?": '0,"No error"',
        }

    def query(self, cmd):
        if cmd not in self.q:
            raise AssertionError(f"unexpected query {cmd!r}")
        return self.q[cmd] + "\n"

    def write(self, cmd):
        if cmd not in self.ALLOWED:
            raise AssertionError(f"state-changing write at start-up: {cmd!r}")
        self.writes.append(cmd)

    def close(self):
        pass


@pytest.fixture
def fake_visa(monkeypatch):
    inst = FakeInst()

    class RM:
        def __init__(self, *a):
            pass

        def open_resource(self, name):
            return inst

        def close(self):
            pass
    mod = types.SimpleNamespace(ResourceManager=RM)
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return inst


def test_real_backend_start_is_queries_only(fake_visa):
    backend = VisaK2450("FAKE::INSTR", terminals="front")
    smu = SourceMeter(backend, Config())
    smu.start(poll=False)
    try:
        assert fake_visa.writes == ["*CLS"]                     # the one allowed write
        st = smu.status()
        assert st.output is True and st.source_function == "current"
        assert st.source_current_set_A == pytest.approx(1.2e-3)
        assert st.voltage_limit_V == 5.0 and st.nplc == 2.0 and st.four_wire is True
        assert st.source_auto_range is False
        assert smu.cfg.hardware.terminals == "rear"             # read, not the ctor's "front"
    finally:
        smu._connected = False                                  # skip the shutdown writes
        backend._inst = None
