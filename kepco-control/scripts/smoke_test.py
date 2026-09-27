"""A quick offline sanity check -- no hardware, no network.

Builds the simulated BOP with its coil, ramps the current up and back down,
shows the clamp, takes one acquisition and checks the output ends OFF:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from kepco.config import Config
from kepco.sim_system import build_sim_system


def wait(pred, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    cfg = Config()
    cfg.ramp.rate_A_per_s = 4.0            # quick, for a smoke test
    supply, backend = build_sim_system(cfg, seed=1)
    events = []
    supply._on_event = lambda lvl, msg: events.append((lvl, msg))

    supply.start()
    print("IDN:", backend.idn())
    assert supply.status().output is False, "output must start OFF"

    supply.set_current(2.0)
    supply.set_output(True)
    t0 = time.monotonic()
    assert wait(lambda: supply.status().output and not supply.status().ramping
                and supply.status().programmed == 2.0), "ramp did not finish"
    print(f"ramped 0 -> 2 A in {time.monotonic() - t0:.2f} s "
          f"(rate {cfg.ramp.rate_A_per_s} A/s)")

    n = supply.acquire()
    assert wait(lambda: supply.status().sample.get("acq_id") == n)
    s = supply.status().sample
    print(f"acquired #{n}: {s['current_A']:.4f} A, {s['voltage_V']:.4f} V "
          f"(coil R = {cfg.sim.load_R_ohm} ohm)")
    assert abs(s["current_A"] - 2.0) < 0.02

    supply.set_current(99.0)               # way over the limit -> clamped
    print(f"asked 99 A, target is {supply.status().current_set_A if wait(lambda: supply.status().current_set_A != 2.0) else '?'} A")
    assert supply.status().current_set_A == cfg.limits.current_max_A

    supply.shutdown()                      # ramps to zero, then OUTP OFF
    assert backend.output_on is False
    print(f"after shutdown: output={backend.output_on}, "
          f"coil current {backend.true_current:.3f} A")
    print(f"{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
