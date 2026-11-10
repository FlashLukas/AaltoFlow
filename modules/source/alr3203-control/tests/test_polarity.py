"""The polarity relay, the bumpless mode switch and the 96 W envelope.

Lukas, 2026-11-09: the relay may switch with the output ON, but only with no
current flowing (5 mA), no extra wait (small magnet); add hysteresis; switch
modes by first taking the limit down to the actual value, then raising the
other quantity to its full range. Stepped by hand on a fake clock; the
SimRelay knows the simulated coil, so a switch under current is COUNTED.
"""

import pytest

from alr3203.backends.sim import NoRelay, SimulatedALR3203
from alr3203.config import Config
from alr3203.supply import BipolarSupply

from conftest import FakeClock, make_rig, run_for


def _on_at(supply, clock, amps, seconds=3.0):
    supply.set_current(amps)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, seconds)


# ---- sign changes ------------------------------------------------------------------

def test_sign_change_goes_through_zero_and_flips_without_current():
    supply, spy, clock, events, relay = make_rig()
    _on_at(supply, clock, 1.0)
    assert spy.inner.true_current == pytest.approx(1.0, abs=0.01)
    assert relay.inverted is False
    supply.set_current(-1.0)
    run_for(supply, clock, 5.0)
    s = supply.status()
    assert relay.inverted is True and relay.flips == [True]
    assert relay.hot_switches == 0                    # never under current
    assert spy.inner.true_current == pytest.approx(1.0, abs=0.01)
    assert s.current_A == pytest.approx(-1.0, abs=0.02)   # signed by the relay
    assert s.programmed == -1.0 and not s.ramping and not s.polarity_switching
    assert s.relay_inverted is True and s.relay_flips == 1
    # the supply itself only ever got magnitudes
    assert all(c[1] >= 0 for c in spy.calls if c[0].startswith("program_"))
    assert any("polarity relay -> INVERTED" in m for _, m in events)
    supply.shutdown()


def test_the_relay_waits_for_the_coil_current_to_decay():
    """A slow coil (L/R = 0.5 s): the programmed value reaches 0 long before
    the current does. The flip must wait for the MEASURED current."""
    cfg = Config()
    cfg.sim.load_L_H = 1.0
    supply, spy, clock, _, relay = make_rig(cfg)
    supply.cfg.ramp.enabled = False                   # step to 0 at once
    _on_at(supply, clock, 1.0, seconds=6.0)
    supply.set_current(-0.5)
    supply.step()
    clock.advance(0.05); supply.step()
    assert supply.status().programmed == 0.0
    assert supply.status().polarity_switching is True
    assert relay.flips == []                          # current still flowing
    run_for(supply, clock, 6.0)
    assert relay.flips == [True] and relay.hot_switches == 0
    supply.shutdown()


def test_the_flip_is_refused_when_the_current_does_not_stop():
    supply, spy, clock, events, relay = make_rig()
    _on_at(supply, clock, 1.0)
    supply.cfg.polarity.zero_timeout_s = 1.0
    spy.inner.measure_current = lambda: 0.2           # a stuck reading: never zero
    supply.set_current(-1.0)
    run_for(supply, clock, 3.0)
    s = supply.status()
    assert relay.flips == []                          # NEVER switched under current
    assert s.current_set_A == 0.0 and s.programmed == 0.0
    assert not s.polarity_switching
    assert any(lvl == "error" and "refused" in m for lvl, m in events)
    supply.shutdown()


def test_deadband_keeps_a_small_opposite_setpoint_at_zero():
    """Hysteresis: relay normal, a setpoint of -3 mA (deadband 5 mA) is output
    as 0 and does not flip; -10 mA does."""
    supply, spy, clock, _, relay = make_rig()
    _on_at(supply, clock, 0.5)
    supply.set_current(-0.003)
    run_for(supply, clock, 2.0)
    assert relay.flips == [] and supply.status().programmed == 0.0
    assert not supply.status().ramping                # arrived: at 0 is the answer
    supply.set_current(-0.010)
    run_for(supply, clock, 2.0)
    assert relay.flips == [True]
    assert supply.status().programmed == pytest.approx(-0.010)
    # and back: +3 mA now stays at 0 on the INVERTED side
    supply.set_current(0.003)
    run_for(supply, clock, 2.0)
    assert relay.flips == [True] and supply.status().programmed == 0.0
    supply.shutdown()


def test_a_relay_that_cannot_be_switched_holds_the_output_at_zero():
    supply, spy, clock, events, relay = make_rig()
    _on_at(supply, clock, 1.0)
    relay.fail = True
    supply.set_current(-1.0)
    run_for(supply, clock, 2.0)
    s = supply.status()
    assert s.programmed == 0.0 and s.relay_error and relay.inverted is False
    assert any(lvl == "error" and "polarity relay" in m for lvl, m in events)
    relay.fail = False                                 # the DAQ service is back
    run_for(supply, clock, 4.0)
    s = supply.status()
    assert relay.inverted is True and s.relay_error == ""
    assert s.programmed == -1.0
    supply.shutdown()


def test_voltage_mode_changes_sign_too():
    supply, spy, clock, _, relay = make_rig()
    supply.set_mode("voltage")
    supply.set_voltage(2.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 2.0)
    supply.set_voltage(-2.0)
    run_for(supply, clock, 3.0)
    s = supply.status()
    assert relay.inverted is True and relay.hot_switches == 0
    assert s.voltage_V == pytest.approx(-2.0, abs=0.02)
    assert s.current_A == pytest.approx(-1.0, abs=0.02)        # 2 V / 2 ohm
    supply.shutdown()


# ---- start-up --------------------------------------------------------------------

def test_a_live_inverted_output_is_adopted_signed_and_untouched():
    cfg = Config()
    cfg.sim.found_output = True
    cfg.sim.found_current_A = 1.5                     # CC at 1.5 A, VOLT 32 = limit
    supply, spy, clock, _, relay = make_rig(cfg, relay_inverted=True)
    run_for(supply, clock, 1.0)
    s = supply.status()
    assert s.mode == "current" and s.output is True
    assert s.current_set_A == -1.5 and s.programmed == -1.5
    assert s.current_A == pytest.approx(-1.5, abs=0.02)
    assert spy.inner.writes == [] and relay.flips == []
    supply.shutdown()


def test_an_unknown_relay_is_trusted_until_a_client_changes_the_setpoint():
    """The DAQ service was down at start: the adopted live output must not be
    ramped to zero just to find out. The first set establishes the relay."""
    cfg = Config()
    cfg.sim.found_output = True
    cfg.sim.found_current_A = 1.0
    supply, spy, clock, events, relay = make_rig(cfg)
    relay.readable = False
    supply.shutdown(keep_outputs=True)
    # a fresh brain on the same coil, relay unreadable
    clock2 = FakeClock()
    sim = SimulatedALR3203(load=cfg.sim, clock=clock2, seed=0)
    relay.supply, relay.readable = sim, False
    brain = BipolarSupply(sim, cfg, relay=relay, clock=clock2)
    msgs = []
    brain._on_event = lambda lvl, m: msgs.append((lvl, m))
    brain.start(poll=False)
    run_for(brain, clock2, 2.0)
    assert brain.status().relay_inverted is None
    assert sim.writes == [] and relay.flips == []       # untouched
    assert any("unknown" in m for _, m in msgs)
    brain.set_current(0.8)                              # a client takes charge
    run_for(brain, clock2, 4.0)
    assert relay.flips == [False] and relay.hot_switches == 0
    assert brain.status().relay_inverted is False
    assert brain.status().programmed == 0.8
    brain.shutdown()


def test_without_a_relay_negative_setpoints_are_clamped_to_zero():
    cfg = Config()
    clock = FakeClock()
    sim = SimulatedALR3203(load=cfg.sim, clock=clock, seed=0)
    supply = BipolarSupply(sim, cfg, relay=NoRelay(), clock=clock)
    supply.start(poll=False)
    assert supply.current_range() == (0.0, 6.0)
    supply.set_current(-2.0)
    assert supply.cfg.output.current_A == 0.0
    supply.shutdown()


# ---- the bumpless mode switch ---------------------------------------------------------

def test_voltage_to_current_mode_live_keeps_the_coil_current():
    supply, spy, clock, events, relay = make_rig()
    supply.set_mode("voltage")
    supply.set_voltage(3.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 3.0)
    i_before = spy.inner.true_current                  # 1.5 A in 2 ohm
    spy.calls.clear()
    supply.set_mode("current")                         # live: no wait without a thread
    supply.step()
    s = supply.status()
    assert s.mode == "current" and s.output is True
    # first the current setpoint down to what flows, THEN the voltage up
    names = [c[0] for c in spy.calls if c[0].startswith("program_")]
    assert names[:2] == ["program_current", "program_voltage"]
    assert [c[1] for c in spy.calls if c[0] == "program_voltage"][0] == 32.0
    assert s.current_set_A == pytest.approx(i_before, abs=0.02)
    run_for(supply, clock, 1.0)
    assert spy.inner.true_current == pytest.approx(i_before, abs=0.02)
    assert supply.status().regulation == "current"
    supply.shutdown()


def test_current_to_voltage_mode_live_on_the_inverted_side():
    supply, spy, clock, _, relay = make_rig()
    _on_at(supply, clock, -1.0, seconds=4.0)
    assert relay.inverted is True
    i_before = spy.inner.true_current
    supply.set_mode("voltage")
    supply.step()
    s = supply.status()
    assert s.mode == "voltage"
    assert s.voltage_set_V == pytest.approx(-2.0, abs=0.03)   # signed: inverted side
    assert spy.inner.read_state()["current_A"] == 6.0         # limit to full range
    run_for(supply, clock, 1.0)
    assert spy.inner.true_current == pytest.approx(i_before, abs=0.02)
    assert relay.flips == [True]                              # no extra flip
    supply.shutdown()


def test_mode_switch_is_refused_while_the_relay_switches():
    cfg = Config()
    cfg.sim.load_L_H = 1.0
    supply, spy, clock, _, relay = make_rig(cfg)
    supply.cfg.ramp.enabled = False
    _on_at(supply, clock, 1.0, seconds=6.0)
    supply.set_current(-1.0)
    supply.step()
    clock.advance(0.05); supply.step()
    assert supply.status().polarity_switching
    with pytest.raises(ValueError, match="polarity"):
        supply.set_mode("voltage")
    supply.shutdown()


# ---- envelope and display values --------------------------------------------------------

def test_the_96_W_envelope_is_reported():
    cfg = Config()
    cfg.sim.load_R_ohm = 4.0
    supply, spy, clock, _, _ = make_rig(cfg)
    supply.set_mode("voltage")
    supply.set_voltage(30.0)              # 30 V into 4 ohm would be 7.5 A, 225 W
    supply.cfg.ramp.rate_V_per_s = 40.0
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 3.0)
    s = supply.status()
    assert s.power_limited is True and s.at_limit is True
    assert s.power_W == pytest.approx(96.0, rel=0.03)
    assert s.resistance_ohm == pytest.approx(4.0, rel=0.03)
    supply.shutdown()


def test_resistance_is_nan_without_current(rig):
    supply, *_ = rig
    import math
    assert math.isnan(supply.status().resistance_ohm)


# (a sweep across zero: tests/test_sweep.py -- it needs the real clock)


def test_a_voltage_limit_set_before_the_switch_is_the_one_used():
    """Blue Box, 2026-11-10: switching voltage -> current with the output live
    set the voltage to the limit the module held (10 V, adopted from the
    supply) before the client's own limit arrived. The order that avoids it:
    set_voltage_limit FIRST -- in voltage mode it is only stored -- then
    set_mode("current"); the switch programs that limit."""
    supply, spy, clock, events, relay = make_rig()
    supply.set_mode("voltage")
    supply.set_voltage(1.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 2.0)
    spy.calls.clear()
    supply.set_voltage_limit(2.0)                       # stored only (voltage mode)
    assert not [c for c in spy.calls if c[0].startswith("program_")]
    supply.set_mode("current")
    supply.step()
    volts = [c[1] for c in spy.calls if c[0] == "program_voltage"]
    assert volts and volts[0] == 2.0 and max(volts) == 2.0
    assert any("voltage setpoint -> 2 V" in m for _, m in events)
    supply.shutdown()


def test_an_asymmetric_envelope_is_announced_at_start():
    """Blue Box, 2026-11-10: [limits] current_max_A = 1.0 alone left -6 A on
    the negative side (current_range() = (-6, 1)). Allowed -- min and max are
    two settings -- but now said at start."""
    cfg = Config()
    cfg.limits.current_max_A = 1.0
    supply, spy, clock, events, relay = make_rig(cfg)       # make_rig starts it
    try:
        assert supply.current_range() == (-6.0, 1.0)
        warns = [m for lvl, m in events if lvl == "warn" and "NOT the same on both" in m]
        assert warns and "current -6 .. +1 A" in warns[0]
    finally:
        supply.shutdown()
    cfg = Config()
    cfg.limits.current_min_A, cfg.limits.current_max_A = -1.0, 1.0
    supply, spy, clock, events, relay = make_rig(cfg)       # make_rig starts it
    try:
        assert supply.current_range() == (-1.0, 1.0)
        assert not [m for lvl, m in events if "NOT the same on both" in m]
    finally:
        supply.shutdown()
