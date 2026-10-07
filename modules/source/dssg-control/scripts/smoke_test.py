"""A quick offline sanity check -- no hardware, no network.

Builds the simulated SG12000L, exercises every control, and shows that the
safety clamp and the unit's own range both work. Run it any time:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dssg.config import Config
from dssg.sim_system import build_sim_system


def wait_for(synth, pred, timeout=2.0):
    """The status is a READ-BACK snapshot from the poll thread: wait for it."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.02)
    raise AssertionError("status never showed the expected values")


def main() -> int:
    cfg = Config()
    synth, backend = build_sim_system(cfg)
    events = []
    synth._on_event = lambda lvl, msg: events.append((lvl, msg))

    synth.start()
    print("IDN:", backend.idn())
    # adopt-on-start: the status shows the simulated box's own state
    s0 = synth.status()
    assert s0.rf_on == cfg.sim.state_rf_on, "RF state must be adopted, not changed"
    assert s0.frequency_Hz == cfg.sim.state_frequency_Hz, "frequency must be adopted"
    lim = synth.limits()
    print(f"effective range: {lim['freq_min_Hz']/1e6:g} .. {lim['freq_max_Hz']/1e9:g} GHz, "
          f"{lim['power_min_dBm']:g} .. {lim['power_max_dBm']:g} dBm")

    synth.set_frequency(2.45e9)
    synth.set_power(-7.3)                      # attenuator -7.5 + the vernier (fine power)
    synth.set_phase(45.0)
    synth.set_reference("internal")
    synth.set_rf(True)
    s = wait_for(synth, lambda s: s.rf_on and s.frequency_Hz == 2.45e9)
    print(f"after set: RF={s.rf_on}  f={s.frequency_Hz/1e6:.3f} MHz  "
          f"P={s.power_dBm} dBm (asked -7.3; attenuator {s.attenuator_dBm}, "
          f"vernier {s.vernier:+d})  phase={s.phase_deg} deg  ref={s.reference}  "
          f"USB={s.usb_volts:.2f} V")
    s = wait_for(synth, lambda s: s.attenuator_dBm == -7.5)
    assert abs(s.power_dBm - -7.3) <= 0.05

    synth.set_power(999.0)                     # above the config ceiling
    synth.set_frequency(40e9)                  # above what the unit can do
    s = wait_for(synth, lambda s: s.power_dBm == lim["power_max_dBm"]
                 and s.frequency_Hz == lim["freq_max_Hz"])
    print(f"after over-range: P={s.power_dBm} dBm, f={s.frequency_Hz/1e9:.3f} GHz")

    synth.shutdown()
    assert backend.read_output() is False, "RF must be off after shutdown"
    assert synth.status().connected is False
    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
