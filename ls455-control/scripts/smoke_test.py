"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from ls455.config import Config
from ls455.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    cfg.acquisition.settle_time_constants = 0.0      # no waiting in a smoke test
    meter, sim = build_sim_system(cfg, realtime=False, seed=1)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    s = meter.status()
    print("IDN:", s.idn, "| probe", s.probe, "| ranges (mT):", s.ranges_mT)

    for _ in range(3):
        meter.poll_once()
    s = meter.status()
    print(f"live: {s.field_mT:.4f} mT (sim field {sim.field_mT:g} mT + offset), "
          f"range {s.range_mT:g} mT")
    assert abs(s.field_mT - sim.field_mT) < 0.5

    n = meter.acquire()
    for _ in range(cfg.acquisition.readings):
        meter.poll_once()
    s = meter.status()
    assert s.acq_id == n and not s.acquiring and s.sample["n"] == cfg.acquisition.readings
    print(f"acquire #{n}: {s.sample['field_mT']:.4f} mT, sd {s.sample['std_mT'] * 1e3:.3f} uT")

    meter.set_range(1e6)                            # beyond the probe -> clamped
    assert meter.status().range_mT == max(s.ranges_mT)
    print("clamp ok:", [m for lvl, m in events if lvl == "warn"][-1])

    meter.shutdown()
    assert meter.status().connected is False
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
