"""A quick offline sanity check -- no hardware, no network.

Builds the simulated SR830, changes settings, steps the input signal and shows
the difference between the LIVE reading (still settling) and an ACQUIRED
sample (waited out); then runs Auto Gain and Auto Phase and provokes an
overload. Run it any time to confirm the package works:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from sr830.config import Config
from sr830.sim_system import build_sim_system


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        time.sleep(0.01)


def main() -> int:
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=0)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start()

    li.set_time_constant("10 ms")
    li.set_slope("24 dB/oct")
    s = li.status()
    print(f"IDN: {s.idn}")
    print(f"tc={s.time_constant}, {s.slope}, sensitivity {s.sensitivity}, "
          f"settles 99 % in {s.settle_s * 1e3:.1f} ms")

    time.sleep(0.5)
    before = li.status().live["r"]
    print(f"live R before the step: {before * 1e3:.4f} mV")

    sim.set_signal(5e-3, 30.0)                   # step the input 2 mV -> 5 mV
    n = li.acquire()
    time.sleep(0.02)
    lag = li.status().live["r"]
    print(f"20 ms after the step, live R: {lag * 1e3:.4f} mV   <- still settling")
    _wait(lambda: not li.status().acquiring)
    smp = li.get_sample()
    print(f"acquired sample #{smp['acq_id']} after {smp['settle_s'] * 1e3:.1f} ms: "
          f"R = {smp['r'] * 1e3:.4f} mV, overload = {smp['overload']}")
    assert smp["acq_id"] == n
    assert abs(smp["r"] - 5e-3) < 0.1e-3, "acquired sample should be settled"
    assert lag < 4.5e-3, "live value should still have been lagging"

    li.set_sensitivity("2 mV")                   # 5 mV on a 2 mV range: overload
    time.sleep(0.2)
    print(f"on the 2 mV range: overload = {li.status().overload}")
    k = li.auto_gain()
    _wait(lambda: not li.status().auto_busy)
    print(f"auto gain #{k}: {li.status().auto_note}")
    assert li.status().sensitivity == "10 mV"
    k = li.auto_phase()
    _wait(lambda: not li.status().auto_busy)
    time.sleep(0.2)
    s = li.status()
    print(f"auto phase #{k}: {s.auto_note}; theta now {s.live['theta_deg']:+.2f} deg")
    assert abs(s.live["theta_deg"]) < 2.0

    li.set_sine_out(99.0)                        # clamp
    assert li.status().sine_out_set_V == cfg.limits.sine_max_V

    li.shutdown()
    assert li.status().connected is False
    assert sim.sine_V == cfg.limits.sine_min_V   # made safe on the way out
    print(f"\n{len(events)} events; warnings: {sum(lvl == 'warn' for lvl, _ in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
