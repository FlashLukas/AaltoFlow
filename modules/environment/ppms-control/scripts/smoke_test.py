"""A quick offline sanity check -- no hardware, no network.

Builds the simulated DynaCool, drives the field and the temperature to a new
setpoint, waits until each is REACHED (the flag a scan waits on), and shows that
the safety clamp works. Run it any time to confirm the package imports and
behaves:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from ppms.config import Config
from ppms.sim_system import build_sim_system


def wait_for(cryo, flag: str, timeout_s: float) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if getattr(cryo.status(), flag):
            return time.monotonic() - t0
        time.sleep(0.05)
    raise SystemExit(f"FAIL: {flag} not reached within {timeout_s} s")


def main() -> int:
    cfg = Config()
    cfg.field.stable_time_s = 0.5          # short holds: this is a smoke test
    cfg.temperature.stable_time_s = 0.5
    cfg.hardware.poll_s = 0.05
    cryo, backend = build_sim_system(cfg, field_mT=0.0, temperature_K=300.0)
    backend.NEAR_S = 0.3

    events = []
    cryo._on_event = lambda lvl, msg: events.append((lvl, msg))
    cryo.start()
    try:
        print("IDN:", cryo.status().idn)
        s = cryo.status()
        print(f"adopted: {s.setpoint_field_mT:g} mT, {s.setpoint_temperature_K:g} K "
              f"(nothing commanded)")

        cryo.set_field(40.0)                    # 40 mT at 22 mT/s: ~1.8 s ramp
        assert cryo.status().field_stable is False, "stale 'reached' after a new setpoint"
        dt = wait_for(cryo, "field_stable", 15.0)
        s = cryo.status()
        print(f"field reached in {dt:.1f} s: {s.measured_field_mT:.3f} mT ({s.field_status})")
        assert abs(s.measured_field_mT - 40.0) <= cfg.field.tolerance_mT

        cryo.set_temperature(299.5)             # 0.5 K at 20 K/min: ~1.5 s
        dt = wait_for(cryo, "temperature_stable", 15.0)
        s = cryo.status()
        print(f"temperature reached in {dt:.1f} s: {s.temperature_K:.3f} K "
              f"({s.temperature_status})")

        cryo.set_field(1e6)                     # far beyond the magnet
        s = cryo.status()
        assert s.setpoint_field_mT == cfg.limits.field_max_mT, s.setpoint_field_mT
        print(f"clamp OK: 1e6 mT -> {s.setpoint_field_mT:g} mT")
        assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
    finally:
        cryo.shutdown()
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
