"""A quick offline sanity check -- no hardware, no network.

Builds the simulated synthesizer, exercises both channels, shows that the
safety clamp works and that the outputs go off on shutdown:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from windfreak.config import Config
from windfreak.sim_system import build_sim_system


def wait_settled(synth, ch, timeout=2.0, **echo):
    """Wait until the channel ECHOES the requested values and only then trust
    its settled flag. Waiting on the flag alone returns at once on the frame
    from BEFORE the command (gotcha #2) -- this script did exactly that once."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = synth.status()
        if all(s[f"{ch}_{k}"] == v for k, v in echo.items()) and s[f"{ch}_settled"]:
            return s
        time.sleep(0.01)
    raise SystemExit(f"channel {ch} did not settle")


def main() -> int:
    cfg = Config()
    synth, backend = build_sim_system(cfg)
    events = []
    synth._on_event = lambda lvl, msg: events.append((lvl, msg))

    synth.start()
    print("IDN:", backend.idn())
    s = synth.status()
    # read-only start: the sim was left with A radiating; it must be ADOPTED
    # (still on, shown as on), and nothing may have been sent
    assert s["a_rf_on"] and not s["b_rf_on"], "start must adopt the instrument's state"
    assert backend.writes == [], "start must not write to the instrument"
    print(f"adopted: A {s['a_frequency_Hz']/1e6:.3f} MHz RF on, reference {s['reference']}")

    synth.set_frequency("a", 2.5e9)
    synth.set_power("a", -5.0)
    synth.set_frequency("b", 2.5e9)
    synth.set_phase("b", 90.0)
    synth.set_rf("a", True)
    synth.set_rf("b", True)
    wait_settled(synth, "a", frequency_Hz=2.5e9, power_dBm=-5.0, rf_on=True)
    s = wait_settled(synth, "b", frequency_Hz=2.5e9, phase_deg=90.0, rf_on=True)
    print(f"A: {s['a_frequency_Hz']/1e6:.3f} MHz {s['a_power_dBm']} dBm "
          f"locked={s['a_locked']} leveled={s['a_leveled']}")
    print(f"B: {s['b_frequency_Hz']/1e6:.3f} MHz phase {s['b_phase_deg']} deg "
          f"locked={s['b_locked']}")
    assert s["a_rf_on"] and s["b_rf_on"] and s["a_locked"] and s["b_locked"]

    synth.set_power("a", 999.0)                 # way above power_max_dBm
    synth.set_frequency("b", 1e12)              # above freq_max_Hz
    wait_settled(synth, "a", power_dBm=cfg.limits.power_max_dBm)
    s = wait_settled(synth, "b", frequency_Hz=cfg.limits.freq_max_Hz)
    print(f"after over-range: A power={s['a_power_dBm']} dBm, "
          f"B freq={s['b_frequency_Hz']/1e9:.3f} GHz, B leveled={s['b_leveled']}")
    assert s["a_power_dBm"] == cfg.limits.power_max_dBm
    assert s["b_frequency_Hz"] == cfg.limits.freq_max_Hz

    synth.shutdown()
    assert not backend.output_on(0) and not backend.output_on(1), "RF left on!"
    print(f"after shutdown: connected={synth.status()['connected']}, both outputs off")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
