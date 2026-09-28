"""A quick offline sanity check -- no hardware, no network.

Builds the simulated TC200 (a heated block), switches the heater on, drives
it to a new setpoint, waits until it is REACHED (the flag a scan waits on),
and shows the safety rules work: the clamp, the wrong-sensor refusal, and the
switch-off at shutdown. The sim's clock is fast-forwarded, so it takes seconds,
not the minutes the block needs. Run it any time:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from tc200.config import Config
from tc200.sim_system import build_sim_system


class FakeClock:
    """Time that moves only when told to, so an 8-minute warm-up takes 0 s."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def main() -> int:
    clock = FakeClock()
    cfg = Config()
    heater, sim = build_sim_system(cfg, temperature_C=22.0, setpoint_C=25.0,
                                   clock=clock, seed=1)
    events = []
    heater._on_event = lambda lvl, msg: events.append((lvl, msg))
    heater.start(poll=False)
    try:
        s = heater.status()
        print("IDN:", s.idn)
        print(f"adopted: {s.setpoint_C:g} C, heater {'ON' if s.enabled else 'off'} "
              f"(nothing commanded)")
        assert not s.enabled

        heater.set_temperature(45.0)
        heater.set_enabled(True)
        waited = 0.0
        while not heater.status().temperature_stable:
            clock.t += 1.0
            waited += 1.0
            heater.poll_once()
            if waited > 3600:
                raise SystemExit("FAIL: 45 C not reached within an hour of sim time")
        s = heater.status()
        print(f"45 C reached after {waited:.0f} s of sim time: {s.temperature_C:.2f} C")

        heater.set_temperature(1e3)
        s = heater.status()
        assert s.setpoint_C == heater.temperature_max(), s.setpoint_C
        print(f"clamp OK: 1000 C -> {s.setpoint_C:g} C "
              f"(limit {cfg.limits.temperature_max_C:g}, TMAX {s.tmax_C:g} - margin)")
        assert any(lvl == "warn" and "clamped" in m for lvl, m in events)

        heater.set_enabled(False)
        sim.sensor = "ptc1000"                  # someone set the wrong sensor
        heater.poll_once()
        try:
            heater.set_enabled(True)
            raise SystemExit("FAIL: enabled with the wrong sensor setting")
        except RuntimeError as exc:
            print("wrong sensor refused OK:", str(exc)[:60], "...")
        sim.sensor = "ptc100"
        heater.set_enabled(True)
    finally:
        heater.shutdown()
    assert sim.enabled is False, "heater still on after shutdown"
    print("shutdown switched the heater OFF")
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
