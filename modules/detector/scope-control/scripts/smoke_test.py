"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

Starts the simulated bench (CH1 a 30 Hz sine, CH2 the same frequency
phase-shifted, triggered on the sync square), checks that start wrote nothing
to the scope, puts CH1 into a physical unit (as for a current probe), acquires
an average and checks the numbers against the simulator's truth: CH1's
amplitude and frequency, and CH2's phase against CH1 -- at two phase shifts,
to see the number follow the signal.

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
        # CH1 as a current probe giving 0.1 V/A -> 10 A per volt
        scope.set_physical("ch1", scale=10.0, unit="A", label="Current")
        scope.set_averages(8)
        for shift in (60.0, -30.0):
            scope.set_sim("ch2_phase_deg", shift)
            smp = acquire(scope)
            amp = smp["ch1"]["amplitude"]
            print(f"CH1 {amp:.3f} A amplitude at {smp['ch1']['frequency']:.2f} Hz; "
                  f"CH2 - CH1 phase {smp['phase_21_deg']:+.1f} deg (sim {shift:+.0f})")
            assert abs(amp - 10.0 * cfg.sim.ch1_amplitude_V) < 0.3, "amplitude"
            assert abs(smp["ch1"]["frequency"] - cfg.sim.frequency_Hz) < 0.2, "frequency"
            assert abs(smp["phase_21_deg"] - shift) < 2.0, "phase"
        st = scope.status()
        print(f"{st['records']} triggered traces at {st['trigger_rate_Hz']:.1f} Hz")
    finally:
        scope.shutdown()
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
