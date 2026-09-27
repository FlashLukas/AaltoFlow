"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

It plugs each simulated head in turn (photodiode, thermal, pyro) and checks
that the brain follows. Output is ASCII only: mission-control captures stdout
through a pipe, where anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm400.config import Config
from pm400.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, seed=1)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    print("IDN:", meter.status().idn)

    # photodiode head: at the laser wavelength the reading is the incident power
    meter.set_wavelength(cfg.sim.laser_nm)
    for _ in range(3):
        meter.poll_once()
    s = meter.status()
    print(f"photodiode: {s.value * 1e3:.4f} mW at {s.wavelength_nm:g} nm "
          f"(laser {cfg.sim.incident_W * 1e3:g} mW)")
    assert s.quantity == "power" and abs(s.value - cfg.sim.incident_W) / cfg.sim.incident_W < 0.05

    n = meter.acquire()
    for _ in range(cfg.acquisition.readings):
        meter.poll_once()
    s = meter.status()
    assert s.acq_id == n and not s.acquiring and s.sample["n"] == cfg.acquisition.readings
    print(f"acquire #{n}: {s.sample['value'] * 1e3:.4f} mW, sd {s.sample['std'] * 1e6:.3f} uW")

    meter.set_wavelength(5000.0)                # outside a Si photodiode -> clamped
    assert meter.status().wavelength_nm == 1100.0
    print("clamp ok:", [m for lvl, m in events if lvl == "warn"][-1])

    # plug in the pyroelectric head
    cfg.sim.head = "pyro"
    meter.check_head()
    meter.poll_once()
    s = meter.status()
    assert s.quantity == "energy" and s.unit == "J" and s.value > 0
    print(f"pyro: {s.value * 1e6:.2f} uJ per pulse, range {s.range * 1e3:g} mJ")

    # and a thermal head
    cfg.sim.head = "thermal"
    meter.check_head()
    s = meter.status()
    assert s.head == "thermal" and s.wavelength_max_nm > 20000
    print(f"thermal: wavelength range {s.wavelength_min_nm:g}..{s.wavelength_max_nm:g} nm")

    meter.shutdown()
    assert meter.status().connected is False
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
