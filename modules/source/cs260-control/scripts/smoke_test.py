"""A quick offline sanity check -- no hardware, no network.

Builds the simulated monochromator (with a fast drive, so it takes a second),
moves it, swaps a grating, and shows that the clamp works:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from cs260.config import Config
from cs260.sim_system import build_sim_system


def wait_arrived(mono, timeout=10.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if not mono.status().moving:
            return mono.status()
        time.sleep(0.02)
    raise SystemExit("FAILED: the move did not finish")


def main() -> int:
    cfg = Config()
    cfg.sim.slew_nm_per_s_at_1200 = 5000.0     # fast drive: a smoke test, not a movie
    cfg.sim.grating_change_s = 0.3
    cfg.motion.poll_s = 0.02
    mono, backend = build_sim_system(cfg)
    events = []
    mono._on_event = lambda lvl, msg: events.append((lvl, msg))

    mono.start()
    print("IDN:", backend.idn())

    mono.set_wavelength(700.0)
    assert mono.status().moving, "moving must be set together with the new target"
    s = wait_arrived(mono)
    print(f"arrived: {s.wavelength_nm:.3f} nm (target {s.target_nm:.3f}), grating {s.grating}")
    assert abs(s.wavelength_nm - 700.0) < 0.05

    mono.set_grating(2)
    s = wait_arrived(mono)
    print(f"after grating swap: grating {s.grating} ({s.grating_lines} l/mm), "
          f"{s.wavelength_nm:.3f} nm, shutter {'open' if s.shutter_open else 'closed'}")
    assert s.grating == 2 and abs(s.wavelength_nm - 700.0) < 0.05 and s.shutter_open

    mono.set_wavelength(99999.0)               # far beyond the grating's range
    s = wait_arrived(mono)
    lo, hi = mono.live_limits()
    print(f"after over-range request: target {s.target_nm:g} nm (range {lo:g}..{hi:g})")
    assert s.target_nm == hi

    mono.shutdown()
    s = mono.status()
    assert s.connected is False
    print(f"after shutdown: connected={s.connected}")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
