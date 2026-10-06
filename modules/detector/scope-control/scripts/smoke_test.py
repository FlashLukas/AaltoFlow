"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

Starts the simulated MOKE bench (a 30 Hz field sine on CH1, a hysteresis loop
in the light intensity on CH2, triggered on the sync square), checks that start
wrote nothing to the scope, puts CH1 into mT, acquires an averaged loop and
checks the coercive field comes out as the simulator's (12 mT) -- then at a
second coercive field, to see the number follow the sample.

Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from scope.config import Config
from scope.sim_system import build_sim_system


def acquire(scope) -> dict:
    n = scope.acquire()
    t_end = time.monotonic() + 30
    while time.monotonic() < t_end:
        st = scope.status()
        if st["acq_id"] == n and not st["acquiring"]:
            return st["sample"]
        time.sleep(0.02)
    raise SystemExit(f"acquisition {n} did not finish")


def main() -> int:
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=1)
    scope.start()
    try:
        assert sim.writes == [], "start must not write to the scope"
        print("IDN:", sim.idn())
        # CH1 is a Hall probe: 0.02 V/mT in the simulator -> 50 mT per volt
        scope.set_physical("ch1", scale=1 / cfg.sim.hall_V_per_mT, unit="mT", label="Field")
        scope.set_averages(8)
        for hc in (12.0, 20.0):
            scope.set_sim("hc_mT", hc)
            smp = acquire(scope)
            loop = smp["loop"]
            print(f"sim Hc {hc:4.1f} mT -> measured Hc {loop['hc']:6.2f} mT, "
                  f"bias {loop['bias']:+.2f} mT, Ms {loop['ms']:.3f} V, "
                  f"squareness {loop['squareness']:.2f}, field "
                  f"{smp['ch1']['frequency']:.2f} Hz")
            assert abs(loop["hc"] - hc) < 0.5, "coercive field not recovered"
        st = scope.status()
        print(f"{st['records']} triggered traces at {st['trigger_rate_Hz']:.1f} Hz")
    finally:
        scope.shutdown()
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
