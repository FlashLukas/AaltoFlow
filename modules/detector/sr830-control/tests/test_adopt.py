"""Start-up ADOPTS the instrument's state and changes nothing (Lukas, 2026-09-27).

The rule, for every module: at start the service READS what the instrument is
doing and shows that; it never writes a setting. For the SR830 this matters
because SINE OUT and the AUX OUTs may be driving something (a modulation coil,
a bias), and because somebody may have tuned sensitivity / time constant at
the front panel.

Two levels are tested:
  * the brain against the simulator, which starts from a NON-default state
    (external reference, harmonic 2, 0.5 V sine, current input, ...), with
    every setter booby-trapped -- start() must not call any of them;
  * the real GPIB backend against a FAKE VISA instrument that answers the
    queries and FAILS on any write other than the three allowed ones
    (OUTX 1, OVRM 1, *CLS), so open() + adoption is checked command by command.
"""

import sys
import types

import pytest

from sr830 import tables
from sr830.config import Config
from sr830.sim_system import build_sim_system

SETTERS = ("set_ref_source", "set_frequency", "set_harmonic", "set_phase",
           "set_trigger", "set_sine_out", "set_input", "set_sensitivity",
           "set_reserve", "set_time_constant", "set_slope", "set_sync",
           "set_aux_out", "auto")


def _left_at_front_panel(sim):
    """A plausible state somebody left the SR830 in -- deliberately NOT the
    config defaults (internal 1 kHz, 10 mV, 30 ms, 24 dB, 4 mV, aux 0 V)."""
    sim.internal = False
    sim.ext_ref_Hz = 137.0
    sim._pll_hz = 137.0
    sim.harmonic = 2
    sim.phase_deg = 45.5
    sim.trigger = 1                       # TTL rising
    sim.sine_V = 0.5
    sim.source, sim.ground, sim.coupling, sim.line = 2, 1, 1, 3   # I1M, ground, DC, both
    sim.sens = 17                         # 1 mV (V) -> 1 nA on I1M
    sim.reserve = 0                       # high
    sim.tc = 9                            # 300 ms
    sim.slope = 0                         # 6 dB/oct
    sim.sync = True
    sim.aux_out = [1.25, -2.0, 0.0, 5.0]


def _trap_setters(sim, writes):
    for name in SETTERS:
        def trap(*a, _n=name, **k):
            writes.append((_n, a))
            raise AssertionError(f"start() wrote to the instrument: {_n}{a}")
        setattr(sim, name, trap)


def test_start_writes_nothing_and_adopts_the_front_panel():
    li, sim = build_sim_system(Config(), seed=3)
    _left_at_front_panel(sim)
    writes = []
    real = {n: getattr(sim, n) for n in SETTERS}
    _trap_setters(sim, writes)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        assert writes == []
        s = li.status()
        assert s.reference_source == "external"
        assert s.freq_set_Hz == pytest.approx(137.0)
        assert s.harmonic == s.harmonic_set == 2
        assert s.phase_deg == s.phase_set_deg == pytest.approx(45.5)
        assert s.trigger == "ttl_rising"
        assert s.sine_out_V == s.sine_out_set_V == pytest.approx(0.5)
        assert (s.input_source, s.input_ground, s.input_coupling, s.line_filter) == \
            ("I1M", "ground", "DC", "both")
        assert s.unit == "A" and s.sensitivity == tables.sens_label(17, "I1M")
        assert s.reserve == "high" and s.time_constant == "300 ms"
        assert s.slope == "6 dB/oct" and s.order == 1 and s.sync_filter is True
        assert s.aux_out_set_V == [1.25, -2.0, 0.0, 5.0]
        assert any("adopted the front panel" in m for _, m in events)
        # the instrument is untouched
        assert sim.sine_V == 0.5 and sim.aux_out == [1.25, -2.0, 0.0, 5.0]
        assert not sim.internal and sim.sens == 17 and sim.tc == 9
    finally:
        for n, f in real.items():         # let shutdown run its (out-of-scope) outputs rule
            setattr(sim, n, f)
        li.shutdown()


def test_describe_follows_the_adopted_state():
    """describe must tell the truth about the ADOPTED state: external reference
    -> freq is an indicator; current input -> X/Y/R in A."""
    pytest.importorskip("zmq")
    from sr830.net.describe import build_manifest
    li, sim = build_sim_system(Config(), seed=3)
    _left_at_front_panel(sim)
    li.start(poll=False)
    try:
        m = {p["id"]: p for p in build_manifest(li)["parameters"]}
        assert m["freq"]["kind"] == "indicator"
        assert m["x"]["unit"] == "A"
    finally:
        li.shutdown()


def test_config_that_only_touches_acquisition_writes_nothing():
    li, sim = build_sim_system(Config(), seed=3)
    _left_at_front_panel(sim)
    li.start(poll=False)
    real = {n: getattr(sim, n) for n in SETTERS}
    writes = []
    _trap_setters(sim, writes)
    try:
        li.cfg.acquisition.average_tc = 3.0
        li.apply_config()                  # what set_config / Settings > Apply do
        assert writes == []
    finally:
        for n, f in real.items():
            setattr(sim, n, f)
        li.shutdown()


def test_explicit_config_change_writes_only_that_setting():
    li, sim = build_sim_system(Config(), seed=3)
    _left_at_front_panel(sim)
    li.start(poll=False)
    calls = []
    for name in SETTERS:
        f = getattr(sim, name)
        setattr(sim, name, lambda *a, _f=f, _n=name: (calls.append(_n), _f(*a))[1])
    try:
        li.cfg.aux_out.out2_V = 3.0
        li.apply_config()
        assert calls == ["set_aux_out"]
        assert sim.aux_out == [1.25, 3.0, 0.0, 5.0] and sim.sine_V == 0.5
    finally:
        li.shutdown()


def test_out_of_limits_front_panel_is_adopted_not_clamped():
    cfg = Config()
    cfg.limits.sine_max_V = 0.1           # this setup protects something on SINE OUT
    li, sim = build_sim_system(cfg, seed=3)
    _left_at_front_panel(sim)             # ... but somebody left it at 0.5 V
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        assert sim.sine_V == 0.5 and li.status().sine_out_V == pytest.approx(0.5)
        assert any(lvl == "warn" and "SINE OUT" in m for lvl, m in events)
    finally:
        li.shutdown()


def test_unrelated_apply_does_not_clamp_an_out_of_limits_front_panel():
    """Review 2026-09-27: apply_config used to clamp EVERY cfg value to
    [limits] before comparing -- so a Settings > Apply that only changed the
    acquisition group moved an adopted 0.5 V SINE OUT down to the limit.
    A value the instrument already has is not an edit; an explicit new value
    is still clamped."""
    cfg = Config()
    cfg.limits.sine_max_V = 0.1
    cfg.limits.aux_out_max_V = 2.0
    li, sim = build_sim_system(cfg, seed=3)
    _left_at_front_panel(sim)             # sine 0.5 V, AUX OUT 4 at 5 V
    li.start(poll=False)
    real = {n: getattr(sim, n) for n in SETTERS}
    writes = []
    _trap_setters(sim, writes)
    try:
        li.cfg.acquisition.average_tc = 3.0
        li.apply_config()
        assert writes == []
        assert li.status().sine_out_set_V == pytest.approx(0.5)
    finally:
        for n, f in real.items():
            setattr(sim, n, f)
    try:
        li.cfg.reference.sine_out_V = 0.8     # an explicit new value: clamped
        li.apply_config()
        assert sim.sine_V == pytest.approx(0.1)
        assert sim.aux_out[3] == 5.0          # untouched, still as found
    finally:
        li.shutdown()


# ---- the real backend, against a fake VISA instrument -------------------------------

ALLOWED_WRITES = {"OUTX 1", "OVRM 1", "*CLS"}


class FakeSR830Visa:
    """Answers the SR830's query forms from a state table; any write that is
    not in ALLOWED_WRITES fails the test."""

    def __init__(self):
        self.timeout = None
        self.read_termination = self.write_termination = None
        self.writes = []
        self.queries = []
        self.state = {"FMOD?": "0", "FREQ?": "137.00", "HARM?": "2", "PHAS?": "45.5",
                      "RSLP?": "1", "SLVL?": "0.500", "ISRC?": "2", "IGND?": "1",
                      "ICPL?": "1", "ILIN?": "3", "SENS?": "17", "RMOD?": "0",
                      "OFLT?": "9", "OFSL?": "0", "SYNC?": "1",
                      "AUXV? 1": "1.250", "AUXV? 2": "-2.000", "AUXV? 3": "0.000",
                      "AUXV? 4": "5.000", "*IDN?": "Stanford_Research_Systems,SR830,s/n00000,ver1.07",
                      "LIAS?": "0", "SNAP?1,2,9": "1.0e-10,2.0e-10,137.00",
                      "SNAP?5,6,7,8": "0.1,0.2,0.3,0.4"}

    def write(self, cmd):
        self.writes.append(cmd)
        if cmd not in ALLOWED_WRITES:
            raise AssertionError(f"state-changing write at start: {cmd!r}")

    def query(self, cmd):
        self.queries.append(cmd)
        return self.state[cmd] + "\n"

    def read_stb(self):
        return 0b10                        # interface ready, nothing executing

    def close(self):
        pass


@pytest.fixture
def fake_visa(monkeypatch):
    inst = FakeSR830Visa()
    mod = types.ModuleType("pyvisa")

    class RM:
        def open_resource(self, name):
            return inst

        def close(self):
            pass

    mod.ResourceManager = RM
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return inst


def test_real_backend_open_and_adopt_send_only_allowed_writes(fake_visa):
    from sr830.backends.visa_sr830 import VisaSR830
    from sr830.lockin import DspLockIn
    li = DspLockIn(VisaSR830("GPIB0::8::INSTR"), Config())
    li.start(poll=False)
    try:
        assert set(fake_visa.writes) <= ALLOWED_WRITES
        assert fake_visa.writes[0] == "OUTX 1"          # replies to GPIB, before any query
        s = li.status()
        assert s.reference_source == "external" and s.harmonic == 2
        assert s.sine_out_V == pytest.approx(0.5) and s.input_source == "I1M"
        assert s.time_constant == "300 ms" and s.slope == "6 dB/oct"
        assert s.aux_out_set_V == [1.25, -2.0, 0.0, 5.0]
        li.poll_once()                                   # polling is queries only too
        assert set(fake_visa.writes) <= ALLOWED_WRITES
        assert li.status().live["freq_Hz"] == pytest.approx(137.0)
    finally:
        # shutdown is out of scope of the adopt rule: stop the outputs-safe
        # writes from tripping the fake
        li.cfg.safety.sine_min_on_stop = False
        li.cfg.safety.aux_out_zero_on_stop = False
        li.shutdown()


def test_a_garbled_reply_refuses_to_start_instead_of_guessing(fake_visa):
    from sr830.backends.visa_sr830 import VisaSR830
    from sr830.lockin import DspLockIn
    fake_visa.state["ISRC?"] = "7"
    li = DspLockIn(VisaSR830("GPIB0::8::INSTR"), Config())
    with pytest.raises(ValueError, match="ISRC"):
        li.start(poll=False)
    assert li.status().connected is False
    assert set(fake_visa.writes) <= ALLOWED_WRITES
