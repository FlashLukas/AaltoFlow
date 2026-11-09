"""Start-up adopts the supply's and the relay's state and changes nothing
(Lukas, 2026-09-27: all modules read the instrument state on startup and
change nothing). Here it is a safety matter: the supply may be driving the
magnet when the service starts, and a re-program would put a step on it.

The simulated supply is set up in NON-default states (cfg.sim.found_*), so the
tests prove the state is really read, not merely equal to our defaults.
"""

import pytest

from alr3203.config import Config
from alr3203.net.describe import build_manifest

from conftest import make_rig, run_for


def _found(output=True, volt=32.0, curr=1.2, inverted=False, **changes):
    cfg = Config()
    cfg.sim.found_output = output
    cfg.sim.found_voltage_V = volt
    cfg.sim.found_current_A = curr
    cfg.safety.shutdown_ramp_s = 0.5
    for k, v in changes.items():
        grp, key = k.split("__")
        setattr(getattr(cfg, grp), key, v)
    return make_rig(cfg, relay_inverted=inverted)


def test_a_live_current_output_is_adopted_not_touched():
    supply, spy, clock, events, relay = _found()
    run_for(supply, clock, 1.0)
    assert spy.inner.writes == [] and relay.flips == []
    s = supply.status()
    assert s.mode == "current" and s.output is True and s.output_request is True
    assert s.current_set_A == 1.2 and s.programmed == 1.2
    assert s.voltage_limit_V == 32.0 and not s.ramping
    assert s.current_A == pytest.approx(1.2, abs=0.02)
    assert supply.cfg.output.current_A == 1.2        # get_config shows the truth
    assert any("adopted, nothing changed" in m for _, m in events)
    supply.shutdown()


def test_a_live_voltage_output_is_adopted_as_voltage_mode():
    # 3 V into 2 ohm = 1.5 A < the 6 A current setpoint: the supply is in CV
    supply, spy, clock, _, _ = _found(volt=3.0, curr=6.0)
    run_for(supply, clock, 1.0)
    s = supply.status()
    assert s.mode == "voltage" and s.voltage_set_V == 3.0 and s.current_limit_A == 6.0
    assert spy.inner.writes == []
    p = {d["id"] for d in build_manifest(supply)["parameters"]}
    assert "voltage" in p and "current" not in p
    supply.shutdown()


def test_an_inverted_relay_makes_the_adopted_setpoint_negative():
    supply, spy, clock, _, relay = _found(inverted=True)
    run_for(supply, clock, 1.0)
    s = supply.status()
    assert s.current_set_A == -1.2 and s.relay_inverted is True
    assert s.current_A == pytest.approx(-1.2, abs=0.02)
    assert relay.flips == [] and spy.inner.writes == []
    supply.shutdown()


def test_an_idle_supply_set_up_for_voltage_is_read_as_voltage_mode():
    # output off, CURR at the full 6 A = the limit -> voltage mode
    supply, spy, clock, _, _ = _found(output=False, volt=5.0, curr=6.0)
    run_for(supply, clock, 0.5)
    s = supply.status()
    assert s.mode == "voltage" and s.output is False and s.voltage_set_V == 5.0
    assert spy.inner.writes == []
    supply.shutdown()


def test_the_ramp_continues_from_the_adopted_value():
    supply, spy, clock, _, _ = _found()
    supply.set_current(1.4)
    supply.step()
    run_for(supply, clock, 0.05)
    vals = [c[1] for c in spy.calls if c[0] == "program_current"]
    assert vals and 1.2 < vals[0] <= 1.4               # from 1.2, not from 0
    supply.shutdown()


def test_an_ini_setpoint_is_not_pushed_at_start():
    supply, spy, clock, _, _ = _found(output__current_A=4.0)
    run_for(supply, clock, 1.0)
    assert supply.status().current_set_A == 1.2        # the supply's, not the ini's
    assert spy.inner.writes == []
    supply.shutdown()


def test_the_watchdog_leaves_an_adopted_output_alone_until_a_client_drives_it():
    supply, spy, clock, events, _ = _found(safety__watchdog_s=0.5)
    run_for(supply, clock, 3.0)
    assert supply.status().output is True              # nobody here drove it
    supply.set_current(1.0)                             # now a client did
    run_for(supply, clock, 4.0)
    assert supply.status().output is False
    assert any("no client" in m for _, m in events)
    supply.shutdown()
