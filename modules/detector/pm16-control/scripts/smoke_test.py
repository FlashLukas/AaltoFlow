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

from pm16.config import Config
from pm16.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, seed=1)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    print("IDN:", meter.status().idn)

    meter.set_wavelength(800.0)                 # the sim laser's wavelength
    for _ in range(3):
        meter.poll_once()
    s = meter.status()
    print(f"live: {s.power_W * 1e3:.4f} mW at {s.wavelength_nm:g} nm (laser {sim.incident_W * 1e3:g} mW)")
    assert abs(s.power_W - sim.incident_W) / sim.incident_W < 0.05

    n = meter.acquire()
    for _ in range(cfg.acquisition.readings):
        meter.poll_once()
    s = meter.status()
    assert s.acq_id == n and not s.acquiring and s.sample["n"] == cfg.acquisition.readings
    print(f"acquire #{n}: {s.sample['power_W'] * 1e3:.4f} mW, sd {s.sample['std_W'] * 1e6:.3f} uW")

    meter.set_wavelength(5000.0)                # outside the head -> clamped
    assert meter.status().wavelength_nm == 1100.0
    print("clamp ok:", [m for lvl, m in events if lvl == "warn"][-1])

    meter.shutdown()
    assert meter.status().connected is False
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
