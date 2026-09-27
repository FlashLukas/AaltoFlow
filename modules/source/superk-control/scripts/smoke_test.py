"""A quick offline sanity check -- no hardware, no network.

Builds the simulated laser, checks the class 4 safety rules (no emission at
start, emission refused with an open interlock, everything off at shutdown),
tunes a line, switches crystal and shows that the clamps work:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from superk.config import Config
from superk.laser import SafetyError
from superk.sim_system import build_sim_system


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.3
    laser, backend = build_sim_system(cfg)
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))

    laser.start()
    s = laser.status()
    print(f"started: emission={s.emission_on} rf={s.rf_on} filter={s.filter} "
          f"range={s.filter_min_nm:g}..{s.filter_max_nm:g} nm")
    assert not s.emission_on and not s.rf_on

    backend.open_interlock()
    try:
        laser.set_emission(True)
        raise AssertionError("emission was allowed with an open interlock")
    except SafetyError as exc:
        print(f"refused as it should be: {exc}")
    backend.close_interlock()
    laser.reset_interlock()

    laser.set_power(30.0)
    laser.set_line(1, 700.0, 60.0)
    laser.set_rf(True)
    laser.set_emission(True)
    assert wait_for(lambda: laser.status().emission_on), "emission never came on"
    s = laser.status()
    print(f"emitting: power={s.power_pct} %  line1={s.wavelength_nm[0]} nm "
          f"@ {s.amplitude_pct[0]} %")

    laser.set_power(99.0)                       # above limits.power_max_pct
    laser.set_filter("IR")                      # line 1 at 700 nm -> clamped to 1100
    assert wait_for(lambda: laser.status().wavelength_nm[0] == 1100.0)
    s = laser.status()
    print(f"after clamp + IR crystal: power={s.power_pct} %  line1={s.wavelength_nm[0]} nm")
    assert s.power_pct == cfg.limits.power_max_pct

    laser.shutdown()
    assert not backend.read_emission() and not backend.read_rf()
    print(f"after shutdown: emission={backend.read_emission()} rf={backend.read_rf()}")
    print(f"{len(events)} events, clamps seen: {sum('clamped' in m or 'moved' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
