"""A quick offline sanity check -- no hardware, no network.

Builds the simulated 2450 with a 1 kohm pretend sample, sources a few volts,
acquires, and shows that the compliance limit and the safety clamps work:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from k2450.config import Config
from k2450.sim_system import build_sim_system


def wait_sample(smu, n, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = smu.status()
        if st.acq_id == n and not st.acquiring:
            return st.sample
        time.sleep(0.01)
    raise TimeoutError("acquisition did not finish")


def main() -> int:
    cfg = Config()
    smu, backend = build_sim_system(cfg, seed=1)
    events = []
    smu._on_event = lambda lvl, msg: events.append((lvl, msg))
    smu.start()
    print("IDN:", backend.idn())
    # the simulated 2450 powers up with the output off, and start-up only
    # adopts: so it is still off, and nothing was written to it
    assert smu.status().output is False, "the sim powers up with the output OFF"
    assert backend.writes == [], f"start-up wrote to the instrument: {backend.writes}"
    smu.set_nplc(0.1)                   # fast readings (an explicit write)

    smu.set_current_limit(0.01)
    smu.set_voltage(1.0)
    smu.set_output(True)
    s = wait_sample(smu, smu.acquire())
    print(f"1 V into 1 kohm: I = {s['current_A'] * 1e3:.4f} mA, "
          f"R = {s['resistance_ohm']:.2f} ohm (2-wire, includes 0.5 ohm leads)")
    assert abs(s["current_A"] - 1e-3) < 2e-6

    # compliance: 5 V into 1 kohm would be 5 mA, the limit is 1 mA
    smu.set_current_limit(1e-3)
    smu.set_voltage(5.0)
    s = wait_sample(smu, smu.acquire())
    print(f"5 V, 1 mA limit: I = {s['current_A'] * 1e3:.4f} mA, V = {s['voltage_V']:.3f} V, "
          f"in compliance = {s['tripped']}")
    assert s["tripped"] and abs(s["voltage_V"] - 1.0) < 0.01

    # the output boxes: 1 A limit confines the voltage to 21 V
    smu.set_voltage(0.0)
    smu.set_current_limit(1.0)
    smu.set_voltage(100.0)
    print(f"100 V asked with a 1 A limit -> {cfg.source.voltage_V:g} V (box clamp)")
    assert cfg.source.voltage_V == 21.0

    smu.shutdown()
    assert backend.get_output() is False
    print(f"after shutdown: output={backend.get_output()}")
    print(f"\n{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
