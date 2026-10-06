"""The brain against the simulated SG12000L: adopt-on-start, clamping to BOTH
envelopes, read-back through the poll thread, lifecycle and RF safety."""

import time

import pytest

from dssg.config import Config
from dssg.sim_system import build_sim_system


def wait_for(synth, pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.01)
    raise AssertionError(f"timed out; last status {synth.status()}")


def _left_on(cfg: Config) -> Config:
    """A box someone left RUNNING from the front panel, with every value
    different from both the sim defaults and the config preset -- so a test
    can tell "adopted" from "pushed"."""
    cfg.sim.state_rf_on = True
    cfg.sim.state_frequency_Hz = 3.2e9
    cfg.sim.state_power_dBm = 2.5
    cfg.sim.state_phase_deg = 45.0
    cfg.sim.state_vernier = 7
    cfg.sim.state_reference = "external"
    return cfg


class RecordingSim:
    """Wraps the simulated unit and records every STATE-CHANGING call. During
    start() there must be none (Lukas's rule, 2026-09-27)."""
    SETTERS = ("set_output", "set_frequency", "set_power", "set_phase",
               "set_vernier", "set_reference", "set_buzzer", "set_display")

    def __init__(self, inner):
        self._inner = inner
        self.calls = []
        self.forbid = False

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self.SETTERS:
            def rec(*a):
                if self.forbid:
                    raise AssertionError(f"{name}{a} during start")
                self.calls.append((name,) + a)
                return attr(*a)
            return rec
        return attr

    def close(self, rf_off=True):
        self.calls.append(("close", rf_off))
        self._inner.close(rf_off)


@pytest.fixture
def synth():
    cfg = _left_on(Config())
    cfg.hardware.poll_hz = 20.0
    s, backend = build_sim_system(cfg)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.events, s.sim = events, backend
    s.start()
    yield s
    s.shutdown()


def test_status_after_start_reflects_the_units_own_state(synth):
    """Adopt, don't push: the status, the brain's desired values and the unit
    all show what the box was ALREADY doing, not the config preset."""
    s = synth.status()
    assert s.connected is True
    assert "SG12000L" in s.idn
    assert s.rf_on is True and synth.sim.read_output() is True
    assert s.frequency_Hz == 3.2e9 != synth.cfg.signal.frequency_Hz
    assert s.power_dBm == 2.5 != synth.cfg.signal.power_dBm
    assert s.phase_deg == 45.0
    assert s.reference == "external"
    assert (synth._freq, synth._power, synth._phase, synth._reference, synth._rf_on) \
        == (3.2e9, 2.5, 45.0, "external", True)
    assert any("adopted" in m for _, m in synth.events)


def test_start_issues_no_state_changing_calls():
    cfg = _left_on(Config())
    s, inner = build_sim_system(cfg)
    rec = RecordingSim(inner)
    rec.forbid = True                       # any setter during start() raises
    s.backend = rec
    s.start()
    try:
        rec.forbid = False
        assert rec.calls == []
        assert inner.read_output() is True  # still on, as we found it
        # pressing Apply in Settings with nothing changed sends nothing either
        s.apply_config()
        assert rec.calls == []
    finally:
        s.shutdown()
    assert ("set_output", False) in rec.calls   # shutdown still turns RF off
    assert inner.read_output() is False


def test_only_a_changed_preset_is_sent():
    cfg = _left_on(Config())
    s, inner = build_sim_system(cfg)
    rec = RecordingSim(inner)
    s.backend = rec
    s.start()
    try:
        cfg.ui.theme = "light"              # unrelated change: no instrument traffic
        s.apply_config()
        assert rec.calls == []
        cfg.signal.power_dBm = -12.0        # the user CHANGED the preset power
        cfg.hardware.mute_buzzer = True     # ...and ticked "mute the buzzer"
        s.apply_config()
        assert rec.calls == [("set_power", -12.0), ("set_buzzer", False)]
        assert inner.read_frequency() == 3.2e9      # frequency untouched
        assert inner._buzzer is False
    finally:
        s.shutdown()


def test_adopted_value_outside_limits_is_left_and_warned():
    cfg = _left_on(Config())
    cfg.sim.state_power_dBm = 8.0           # above the +5 dBm ceiling
    s, backend = build_sim_system(cfg)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.start()
    try:
        assert s.status().power_dBm == 8.0 and backend.read_power() == 8.0
        assert any(lvl == "warn" and "outside your limits" in m for lvl, m in events)
        s.set_power(9.0)                    # the NEXT set is clamped
        assert s._power == cfg.limits.power_max_dBm
    finally:
        s.shutdown()


def test_set_and_read_back(synth):
    synth.set_frequency(2.0e9)
    synth.set_power(-7.0)
    synth.set_phase(90.0)
    synth.set_reference("internal")
    synth.set_rf(True)
    s = wait_for(synth, lambda s: s.rf_on and s.frequency_Hz == 2.0e9
                 and s.reference == "internal")
    assert s.power_dBm == -7.0
    assert s.phase_deg == 90.0


def test_power_readback_is_quantised_to_the_attenuator_step(synth):
    synth.set_power(-7.3)
    s = wait_for(synth, lambda s: s.power_dBm != 2.5)       # 2.5 = adopted
    assert s.power_dBm == -7.5              # 0.5 dB step attenuator


def test_power_clamped_to_config_ceiling(synth):
    synth.set_power(1000.0)
    assert synth._power == synth.cfg.limits.power_max_dBm
    assert any("clamped" in m for lvl, m in synth.events if lvl == "warn")


def test_power_floor_is_the_units_own_minimum(synth):
    """cfg says -40 dBm, the unit says -21.5: the narrower one wins."""
    synth.set_power(-1000.0)
    lim = synth.limits()
    assert lim["power_min_dBm"] == synth.cfg.sim.power_min_dBm
    assert synth._power == lim["power_min_dBm"]


def test_frequency_clamped_to_the_units_range(synth):
    synth.set_frequency(40e9)
    assert synth._freq == synth.cfg.sim.freq_max_Hz        # 12 GHz, not cfg's 13
    synth.set_frequency(1.0)
    assert synth._freq == synth.cfg.limits.freq_min_Hz


def test_phase_clamped(synth):
    synth.set_phase(1000.0)
    assert synth._phase == synth.cfg.limits.phase_max_deg


def test_bad_reference_refused(synth):
    with pytest.raises(ValueError):
        synth.set_reference("gps")


def test_phase_refused_on_a_unit_without_it():
    cfg = Config()
    cfg.sim.has_phase = False
    s, _ = build_sim_system(cfg)
    s.start()
    try:
        assert s.has_phase() is False
        with pytest.raises(ValueError):
            s.set_phase(10.0)
        assert s.status().has_phase is False
    finally:
        s.shutdown()


def test_status_never_touches_hardware(synth):
    """status() returns the snapshot: with the backend broken it still answers."""
    synth._stop.set()                       # stop the poller so only status() runs
    time.sleep(0.1)

    def boom(*a, **k):
        raise AssertionError("status() called the hardware")
    synth.sim.read_frequency = boom
    for _ in range(5):
        synth.status()


def test_readback_failure_is_reported_not_fatal(synth):
    def broken():
        raise IOError("USB unplugged")
    synth.sim.read_power = broken
    s = wait_for(synth, lambda s: s.hw_error != "")
    assert "USB unplugged" in s.hw_error
    assert s.connected is True
    assert any(lvl == "error" for lvl, _ in synth.events)


def test_setters_do_not_write_the_snapshot(synth):
    """gotcha #1: the snapshot is rebuilt by the poller, never edited in place."""
    synth._stop.set()
    time.sleep(0.1)                         # freeze the poller
    before = synth.status()
    synth.set_frequency(3e9)
    assert synth.status() is before
    assert before.frequency_Hz != 3e9


def test_shutdown_turns_rf_off_and_is_idempotent():
    cfg = Config()
    s, backend = build_sim_system(cfg)
    s.start()
    s.set_rf(True)
    assert backend.read_output() is True
    s.shutdown()
    assert backend.read_output() is False
    assert s.status().connected is False
    s.shutdown()                            # a second call must not raise


def test_shutdown_keep_outputs_leaves_rf_as_it_is():
    s, backend = build_sim_system(Config())
    s.start()
    s.set_rf(True)
    calls = []
    backend.set_output = lambda on: calls.append(on)   # spy: any RF switch
    s.shutdown(keep_outputs=True)
    assert calls == [] and backend.read_output() is True
    assert backend._open is False and s.status().connected is False


def test_apply_config_reclamps_to_new_limits(synth):
    synth.set_power(4.0)
    synth.cfg.limits.power_max_dBm = 0.0
    synth.apply_config()
    assert synth._power == 0.0
    s = wait_for(synth, lambda s: s.power_dBm == 0.0 and s.power_max_dBm == 0.0)
    assert s.power_max_dBm == 0.0


def test_failed_start_closes_the_port_again():
    """open() succeeded, then a query failed: the brain must release the
    backend instead of leaving the COM port held by a dying process -- and,
    having never taken control, leave the unit's RF exactly as it was."""
    cfg = _left_on(Config())
    s, backend = build_sim_system(cfg)

    def broken():
        raise TimeoutError("no reply to FREQ:CW?")
    backend.read_frequency = broken
    with pytest.raises(TimeoutError):
        s.start()
    assert backend._open is False
    assert backend.read_output() is True        # untouched
    assert s.status().connected is False


def test_an_off_step_power_is_rounded_before_it_is_sent(synth, monkeypatch):
    """Lab PC 2026-10-06: the SG12000L IGNORES "POWER -13.75" (off the 0.5 dB
    attenuator grid) and stayed at -20 dBm; a scan waited 60 s for it. Only
    on-step values are sent now, and the log says when a request moved."""
    sent = []
    real = synth.backend.set_power
    monkeypatch.setattr(synth.backend, "set_power", lambda v: (sent.append(v), real(v)))
    synth.set_power(-13.75)
    assert sent[-1] == -14.0                          # nearest step (half -> even)
    synth.set_power(-7.3)
    assert sent[-1] == -7.5
    synth.set_power(-12.5)
    assert sent[-1] == -12.5                          # on the grid: unchanged
    assert any("asked -7.3" in m for _l, m in synth.events)


def test_describe_declares_the_power_resolution():
    from dssg.net.describe import build_manifest
    from dssg.sim_system import build_sim_system
    s, _b = build_sim_system(Config())
    d = next(p for p in build_manifest(s)["parameters"] if p["id"] == "power")
    assert d["resolution"] == 0.5


# ---- VERNIER (fine power trim, raw counts) -------------------------------

def test_vernier_is_adopted_not_pushed(synth):
    """The box was left at vernier 7 (_left_on): start reads it, writes nothing."""
    assert synth.has_vernier() is True
    assert synth._vernier == 7
    s = synth.status()
    assert s.has_vernier is True and s.vernier == 7 and isinstance(s.vernier, int)


def test_vernier_set_and_read_back(synth):
    synth.set_vernier(-4)
    s = wait_for(synth, lambda s: s.vernier == -4)
    assert synth.sim.read_vernier() == -4
    # the simulator's output level moves with it; POWER? does not (separate knob)
    assert synth.sim.output_dBm() == pytest.approx(
        s.power_dBm + synth.sim.SIM_DB_PER_COUNT * -4)
    assert ("info", "vernier = -4") in synth.events


def test_vernier_rounds_and_clamps_with_a_warning(synth):
    synth.set_vernier(2.6)
    assert synth._vernier == 3
    synth.set_vernier(1000)
    assert synth._vernier == synth.cfg.limits.vernier_max
    synth.set_vernier(-1000)
    assert synth._vernier == synth.cfg.limits.vernier_min
    warns = [m for lvl, m in synth.events if lvl == "warn" and "vernier clamped" in m]
    assert len(warns) == 2


def test_vernier_refused_on_a_unit_without_it():
    cfg = Config()
    cfg.sim.has_vernier = False
    s, inner = build_sim_system(cfg)
    s.start()
    try:
        assert s.has_vernier() is False
        with pytest.raises(ValueError):
            s.set_vernier(1)
        assert s.status().has_vernier is False and s.status().vernier == 0
    finally:
        s.shutdown()


def test_narrower_vernier_limits_reclamp_the_unit(synth):
    synth.set_vernier(20)
    synth.cfg.limits.vernier_max = 5
    synth.apply_config()
    assert synth._vernier == 5
    wait_for(synth, lambda s: s.vernier == 5)
