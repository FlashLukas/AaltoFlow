"""A quick offline sanity check -- no hardware, no network.

Builds the simulated generator, exercises the four controls, and shows that the
safety clamp works. Run it any time to confirm the package imports and behaves:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from smb.config import Config
from smb.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    gen, backend = build_sim_system(cfg)

    events = []
    gen._on_event = lambda lvl, msg: events.append((lvl, msg))

    gen.start()
    print("IDN:", backend.idn())

    gen.set_frequency(1.5e9)
    gen.set_power(-10.0)
    gen.set_phase(45.0)
    gen.set_rf(True)

    s = gen.status()
    print(f"after set: RF={s.rf_on}  freq={s.frequency_Hz/1e6:.3f} MHz  "
          f"power={s.power_dBm} dBm  phase={s.phase_deg} deg")
    assert s.rf_on is True
    assert s.frequency_Hz == 1.5e9
    assert s.power_dBm == -10.0
    assert s.phase_deg == 45.0

    # push past the safety limits -> should clamp
    gen.set_power(999.0)                       # way above power_max_dBm
    gen.set_frequency(1e12)                    # above freq_max_Hz
    s = gen.status()
    print(f"after over-range: power={s.power_dBm} dBm (clamped to {cfg.limits.power_max_dBm}), "
          f"freq={s.frequency_Hz/1e9:.3f} GHz (clamped to {cfg.limits.freq_max_Hz/1e9:.3f})")
    assert s.power_dBm == cfg.limits.power_max_dBm
    assert s.frequency_Hz == cfg.limits.freq_max_Hz

    gen.shutdown()
    s = gen.status()
    assert s.connected is False
    print(f"after shutdown: connected={s.connected}")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
