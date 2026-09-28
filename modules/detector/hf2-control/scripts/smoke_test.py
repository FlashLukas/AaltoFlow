"""A quick offline sanity check -- no hardware, no network.

Builds the simulated lock-in, changes the settings, steps the input signal and
shows the difference between the LIVE reading (still settling) and an ACQUIRED
sample (waited out). Run it any time to confirm the package works:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from hf2.config import Config
from hf2.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=0)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start()

    li.set_time_constant(1, 0.02)
    li.set_order(1, 4)
    li.set_reference(2, "internal")
    li.set_frequency(2, sim.ext_ref_Hz[1])      # tune ch2 onto its signal by hand
    s = li.status()
    print(f"IDN: {s.idn}")
    print(f"ch1: tc={s.tc_s[0]} s, order {s.order[0]}, settles 99 % in {s.settle_s[0] * 1e3:.1f} ms")

    time.sleep(1.0)
    before = li.status().live["r"][0]
    print(f"ch1 live R before the step: {before * 1e3:.4f} mV")

    sim.set_signal(0, 5e-3, 30.0)                # step the input 2 mV -> 5 mV
    n = li.acquire()
    time.sleep(0.03)
    lag = li.status().live["r"][0]
    print(f"30 ms after the step, live R: {lag * 1e3:.4f} mV   <- still settling")
    while li.status().acquiring:
        time.sleep(0.01)
    smp = li.get_sample()
    print(f"acquired sample #{smp['acq_id']} after {smp['settle_s'] * 1e3:.1f} ms: "
          f"R = {smp['r'][0] * 1e3:.4f} mV")
    assert smp["acq_id"] == n
    assert abs(smp["r"][0] - 5e-3) < 0.1e-3, "acquired sample should be settled"
    assert lag < 4e-3, "live value should still have been lagging"
    print(f"ch2 (internal, tuned): R = {smp['r'][1] * 1e3:.4f} mV, "
          f"aux = {smp['aux_in'][0]:+.3f} / {smp['aux_in'][1]:+.3f} V")

    li.set_time_constant(1, 1e9)                  # clamp
    assert li.status().tc_set_s[0] == cfg.limits.tc_max_s

    li.shutdown()
    assert li.status().connected is False
    print(f"\n{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
