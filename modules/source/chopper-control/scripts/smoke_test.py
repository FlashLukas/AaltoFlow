"""A quick offline sanity check -- no hardware, no network.

Builds the simulated chopper, lets it lock, steps the frequency and waits for
the new lock, shows the clamp and the standby-only rule:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from chopper.config import Config
from chopper.sim_system import build_sim_system


def wait_locked(ch, timeout=15.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if ch.status().locked:
            return time.monotonic() - t0
        time.sleep(0.05)
    raise AssertionError("the wheel never locked")


def main() -> int:
    cfg = Config()
    ch, backend = build_sim_system(cfg)
    events = []
    ch._on_event = lambda lvl, msg: events.append((lvl, msg))

    ch.start()
    s = ch.status()
    print(f"IDN: {s.idn}")
    print(f"adopted: blade={s.blade} ref={s.ref_mode} out={s.output_mode} "
          f"f={s.setpoint_frequency_Hz:g} Hz running={s.enabled}")
    print(f"locked after {wait_locked(ch):.2f} s")

    ch.set_frequency(400.0)
    assert ch.status().locked is False, "a new setpoint must clear the lock at once"
    dt = wait_locked(ch)
    s = ch.status()
    print(f"400 Hz: locked after {dt:.2f} s, measured {s.frequency_Hz:.2f} Hz")
    assert abs(s.frequency_Hz - 400.0) < 2.0

    got = ch.set_frequency(1e6)                # far above the blade's range
    print(f"asked 1 MHz, got {got:g} Hz (limit {ch.freq_limits()})")
    assert got == ch.freq_limits()[1]

    try:
        ch.set_blade("MC1F60")
        raise AssertionError("blade change while running must be refused")
    except ValueError as exc:
        print(f"refused as expected: {exc}")
    ch.set_enable(False)
    ch.set_blade("MC1F60")
    s = ch.status()
    print(f"MC1F60: ref={s.ref_mode} out={s.output_mode} range={ch.freq_limits()}")
    assert ch.freq_limits() == (120.0, 6000.0)

    ch.shutdown()
    assert ch.status().connected is False
    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
