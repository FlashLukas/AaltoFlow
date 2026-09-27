"""Start-up adopts the instrument's state and changes nothing (Lukas,
2026-09-27: all modules read the instrument state on startup and change
nothing).

For this module it is a safety matter, not a nicety: the BOP is the SAME
physical unit clMag-control drives, and it may be pushing amperes through a
coil when this service starts. Switching it off, re-programming it or changing
its mode would put a step on that coil.

The simulated BOP is set up in a NON-default state (cfg.sim.found_*), so the
tests prove the state is really read, not merely equal to our defaults.
"""

import time

import pytest

from kepco.backends.sim import SimulatedBOP
from kepco.config import Config
from kepco.net.describe import build_manifest
from kepco.sim_system import build_sim_system
from kepco.supply import BipolarSupply

from conftest import FakeClock, run_for


def _found(mode="current", output=True, current_A=1.2, voltage_V=8.0, **cfg_changes):
    """A brain on a simulated BOP that is FOUND in the given state.
    cfg_changes: group__field=value, e.g. limits__current_max_A=1.0."""
    cfg = Config()
    cfg.sim.found_mode = mode
    cfg.sim.found_output = output
    cfg.sim.found_current_A = current_A
    cfg.sim.found_voltage_V = voltage_V
    cfg.safety.shutdown_ramp_s = 0.5          # keep the tests' shutdown quick
    for k, v in cfg_changes.items():
        grp, key = k.split("__")
        setattr(getattr(cfg, grp), key, v)
    clock = FakeClock()
    sim = SimulatedBOP(load=cfg.sim, clock=clock, seed=0)
    supply = BipolarSupply(sim, cfg, clock=clock)
    events = []
    supply._on_event = lambda lvl, msg: events.append((lvl, msg))
    return supply, sim, clock, events


def test_a_live_current_output_is_adopted_not_touched():
    supply, sim, clock, events = _found()
    supply.start(poll=False)
    run_for(supply, clock, 1.0)                 # 20 worker steps
    assert sim.writes == []                     # not ONE state-changing write
    assert sim.output_on is True
    s = supply.status()
    assert s.mode == "current"
    assert s.output is True and s.output_request is True
    assert s.current_set_A == 1.2 and s.programmed == 1.2
    assert s.voltage_limit_V == 8.0
    assert s.ramping is False
    assert s.current_A == pytest.approx(1.2, abs=0.01)   # the coil really carries it
    # the config mirrors the truth, so Save config / get_config show it
    assert supply.cfg.output.current_A == 1.2
    assert supply.cfg.output.voltage_limit_V == 8.0
    assert any("adopted" in m for _, m in events)
    supply.shutdown()


def test_the_ramp_continues_from_the_adopted_value():
    supply, sim, clock, _ = _found()
    supply.cfg.ramp.rate_A_per_s = 1.0
    supply.start(poll=False)
    supply.step()
    supply.set_current(2.0)
    run_for(supply, clock, 0.05, dt=0.05)
    writes = [w[1] for w in sim.writes if w[0] == "program_current"]
    assert writes[0] == pytest.approx(1.25)     # 1.2 + 1 A/s * 50 ms, not 0
    assert all(w[0] != "set_output" for w in sim.writes)
    run_for(supply, clock, 1.5)
    assert supply.status().programmed == 2.0
    supply.shutdown()


def test_voltage_mode_is_adopted_and_describe_follows():
    supply, sim, clock, _ = _found(mode="voltage", output=True,
                                   voltage_V=3.0, current_A=2.0)
    supply.start(poll=False)
    run_for(supply, clock, 0.5)
    assert sim.writes == []
    s = supply.status()
    assert s.mode == "voltage" and s.output is True
    assert s.voltage_set_V == 3.0 and s.programmed == 3.0
    assert s.current_limit_A == 2.0
    assert s.voltage_V == pytest.approx(3.0, abs=0.02)
    ids = {p["id"] for p in build_manifest(supply)["parameters"]}
    assert "voltage" in ids and "current_limit" in ids and "current" not in ids
    supply.shutdown()


def test_describe_revision_differs_with_the_adopted_mode():
    a, *_ = _found(mode="current", output=False)
    b, *_ = _found(mode="voltage", output=False)
    a.start(poll=False)
    b.start(poll=False)
    assert build_manifest(a)["revision"] != build_manifest(b)["revision"]
    a.shutdown()
    b.shutdown()


def test_a_found_value_outside_the_envelope_is_reported_not_clamped():
    # the user narrowed the envelope to 1 A, but the BOP is found at 1.5 A:
    # clamping would ramp it down at once -- a change nobody asked for
    supply, sim, clock, events = _found(current_A=1.5, limits__current_max_A=1.0)
    supply.start(poll=False)
    run_for(supply, clock, 0.5)
    assert sim.writes == []
    assert supply.status().current_set_A == 1.5
    assert any(lvl == "warn" and "outside" in m for lvl, m in events)
    supply.shutdown()


def test_the_mode_of_an_adopted_live_output_cannot_be_changed():
    supply, sim, clock, _ = _found()
    supply.start(poll=False)
    with pytest.raises(ValueError, match="output off"):
        supply.set_mode("voltage")
    supply.shutdown()


def test_an_ini_output_setpoint_is_not_pushed_at_start():
    """[output] values of a loaded config are defaults, not commands."""
    supply, sim, clock, _ = _found(output=False, current_A=0.3,
                                   output__current_A=4.0,
                                   output__voltage_limit_V=15.0)
    supply.start(poll=False)
    run_for(supply, clock, 0.5)
    assert sim.writes == []
    s = supply.status()
    assert s.current_set_A == 0.3 and s.voltage_limit_V == 8.0
    supply.shutdown()


def test_start_refuses_an_instrument_it_cannot_read():
    supply, sim, clock, _ = _found()

    def broken():
        raise TimeoutError("VI_ERROR_TMO")
    sim.read_state = broken
    with pytest.raises(RuntimeError, match="unknown state"):
        supply.start(poll=False)
    assert sim.writes == [] and sim.output_on is True   # left exactly as found
    assert supply.status().connected is False


def test_the_worker_thread_also_writes_nothing():
    cfg = Config()
    cfg.sim.found_output = True
    cfg.sim.found_current_A = -0.7
    cfg.safety.shutdown_ramp_s = 0.5
    supply, sim = build_sim_system(cfg, seed=0)
    supply.start()                              # the real worker thread
    time.sleep(0.4)
    assert sim.writes == []
    assert supply.status().current_set_A == -0.7
    assert supply.status().output is True
    supply.shutdown()


def test_the_watchdog_leaves_an_adopted_output_alone_until_a_client_drives_it():
    """An output found live was energised by someone else. The lost-client
    watchdog must not ramp it down just because nobody has spoken to this
    service yet -- that would change the instrument at start by the back door.
    Once a client sends a setpoint, the watchdog guards it as usual."""
    supply, sim, clock, events = _found(safety__watchdog_s=1.0)
    supply.start(poll=False)
    run_for(supply, clock, 3.0)                 # 3x the watchdog, total silence
    assert sim.writes == []
    assert supply.status().output is True
    supply.set_current(1.0)                     # a client takes charge
    supply.touch()
    run_for(supply, clock, 6.0)                 # ...and then goes silent
    assert supply.status().output is False
    assert any("no client" in m for _, m in events)
