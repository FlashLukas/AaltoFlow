"""A quick offline sanity check -- no hardware, no network.

Builds the simulated 7230, changes the settings, steps the input signal and
shows the difference between the LIVE reading (still settling) and an ACQUIRED
sample (waited out), then runs auto-measure. Run it any time to confirm the
package works:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from sr7230.config import Config
from sr7230.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=0)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start()

    li.set_sensitivity("10 mV")
    li.set_time_constant(0.02)          # -> 20 ms, in the table
    li.set_slope(24)
    s = li.status()
    print(f"IDN: {s.idn}")
    print(f"tc={s.tc_s} s, {s.slope}, settles 99 % in {s.settle_s * 1e3:.1f} ms, "
          f"full scale {s.sensitivity}")

    time.sleep(1.0)
    before = li.status().live["r"]
    print(f"live R before the step: {before * 1e3:.4f} mV")

    sim.set_signal(5e-3, 30.0)           # step the input 2 mV -> 5 mV
    n = li.acquire()
    time.sleep(0.03)
    lag = li.status().live["r"]
    print(f"30 ms after the step, live R: {lag * 1e3:.4f} mV   <- still settling")
    while li.status().acquiring:
        time.sleep(0.01)
    smp = li.get_sample()
    print(f"acquired sample #{smp['acq_id']} after {smp['settle_s'] * 1e3:.1f} ms: "
          f"R = {smp['r'] * 1e3:.4f} mV, overload {smp['overload']}")
    assert smp["acq_id"] == n
    assert abs(smp["r"] - 5e-3) < 0.1e-3, "acquired sample should be settled"
    assert lag < 4e-3, "live value should still have been lagging"

    a = li.auto("auto_measure")
    while li.status().auto_busy:
        time.sleep(0.01)
    s = li.status()
    print(f"auto-measure #{a}: sensitivity {s.sensitivity}, phase {s.phase_deg:+.2f} deg")
    assert s.sensitivity == "10 mV" and abs(s.phase_deg - 30.0) < 1.0

    li.set_amplitude(99.0)               # clamp: OSC OUT envelope
    assert li.status().amplitude_V == cfg.limits.amplitude_max_V

    li.shutdown()
    assert li.status().connected is False
    assert sim.osc_amp == 0.0, "OSC OUT must be back at 0 V after shutdown"
    print(f"\n{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
