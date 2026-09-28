"""A quick offline sanity check -- no hardware, no network.

Builds the simulated TG, exercises the three controls, shows that the safety
clamp works and that a command is refused while a network-analyser sweep holds
the TG. Run it any time to confirm the package imports and behaves:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from shsg.config import Config
from shsg.generator import Refused
from shsg.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    gen, backend = build_sim_system(cfg)

    events = []
    gen._on_event = lambda lvl, msg: events.append((lvl, msg))

    gen.start()
    print("IDN:", backend.idn())

    gen.set_frequency(1.5e9)
    gen.set_power(-15.0)
    gen.set_rf(True)

    s = gen.status()
    print(f"after set: CW={s.rf_on}  freq={s.frequency_Hz/1e6:.3f} MHz  "
          f"level={s.power_dBm} dBm")
    assert s.rf_on is True
    assert s.frequency_Hz == 1.5e9
    assert s.power_dBm == -15.0

    # push past the safety limits -> should clamp
    gen.set_power(999.0)
    gen.set_frequency(1e12)
    s = gen.status()
    print(f"after over-range: level={s.power_dBm} dBm (clamped to {cfg.limits.power_max_dBm}), "
          f"freq={s.frequency_Hz/1e9:.3f} GHz (clamped to {cfg.limits.freq_max_Hz/1e9:.3f})")
    assert s.power_dBm == cfg.limits.power_max_dBm
    assert s.frequency_Hz == cfg.limits.freq_max_Hz

    # an SNA sweep takes the TG: commands are refused, status says tg_busy
    backend.simulate_sweep(True)
    try:
        gen.set_frequency(2e9)
    except Refused as exc:
        print(f"while busy: refused ({exc})")
    else:
        raise AssertionError("a command went through while the TG was busy")
    assert gen.status().tg_busy is True
    backend.simulate_sweep(False)

    gen.shutdown()
    assert backend.read_state()["parked"] is True       # off_on_shutdown default: park
    assert gen.status().connected is False
    print("after shutdown: TG parked (the TG44A cannot be silenced), disconnected")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
