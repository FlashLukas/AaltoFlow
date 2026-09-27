"""A quick offline sanity check -- no hardware, no network.

Builds the simulated 8648D, exercises the controls, and shows that the safety
clamps (including the frequency-dependent ceiling) and the reverse-power
protection behave. Run it any time to confirm the package imports and works:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from hp8648.config import Config
from hp8648.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    src, backend = build_sim_system(cfg)

    events = []
    src._on_event = lambda lvl, msg: events.append((lvl, msg))

    src.start()
    s = src.status()
    print("IDN:", s.idn)
    assert s.rf_on is False, "RF must be off after start"

    src.set_frequency(1.5e9)
    src.set_power(-10.0)
    src.set_rf(True)
    assert src.wait_idle(2.0)
    s = src.status()
    print(f"after set: RF={s.rf_on}  freq={s.frequency_Hz/1e6:.5f} MHz  "
          f"power={s.power_dBm} dBm  ceiling={s.power_ceiling_dBm} dBm")
    assert s.rf_on is True and s.frequency_Hz == 1.5e9 and s.power_dBm == -10.0

    # +12 dBm is inside spec at 1.5 GHz; moving above 2500 MHz lowers it to +10
    src.set_power(12.0)
    src.set_frequency(3.0e9)
    assert src.wait_idle(2.0)
    s = src.status()
    print(f"above 2500 MHz: power={s.power_dBm} dBm (ceiling {s.power_ceiling_dBm})")
    assert s.power_dBm == 10.0

    src.set_power(999.0)
    src.set_frequency(1e12)
    assert src.wait_idle(2.0)
    s = src.status()
    print(f"over-range: power={s.power_dBm} dBm, freq={s.frequency_Hz/1e9:.3f} GHz")
    assert s.power_dBm == s.power_ceiling_dBm
    assert s.frequency_Hz == cfg.limits.freq_max_Hz

    backend.inject_reverse_power()
    src._wake.set()
    src.wait_idle(1.0)
    import time
    time.sleep(0.3)
    s = src.status()
    print(f"reverse power: tripped={s.rpp_tripped}  RF={s.rf_on}  desired RF={s.rf_set}")
    assert s.rpp_tripped and not s.rf_on and not s.rf_set

    src.shutdown()
    assert backend.read_output() is False
    print(f"after shutdown: connected={src.status().connected}")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m or 'lowered' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
