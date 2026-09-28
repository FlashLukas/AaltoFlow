"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, speed, an absolute and a
relative move, the angle wrap, a clamp, homing and the user zero.  Prints a
short report and exits non-zero if anything looks wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elliptec.config import Config  # noqa: E402
from elliptec.sim_system import build_sim_system  # noqa: E402


def wait_idle(brain, axis=0, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not brain.status().moving[axis]:
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()
    print(f"  start angle {brain.status().angle_deg[0]:.3f} deg")

    print("move to 90 deg")
    brain.move_abs(0, 90.0)
    assert brain.status().moving[0], "moving must show at once (move_id guard)"
    assert wait_idle(brain)
    a = brain.status().angle_deg[0]
    assert abs(a - 90.0) < 0.01, a
    print(f"  arrived at {a:.3f} deg")

    print("relative -100 deg (crosses 0)")
    brain.move_rel(0, -100.0)
    assert wait_idle(brain)
    a = brain.status().angle_deg[0]
    assert abs(a - 350.0) < 0.01, a
    print(f"  now {a:.3f} deg")

    print("angle window 10..170 deg: a request of 200 deg is clamped")
    cfg.limits.min_angle_deg, cfg.limits.max_angle_deg = 10.0, 170.0
    r = brain.move_abs(0, 200.0)
    assert r["target"] == 170.0, r
    assert wait_idle(brain)
    cfg.limits.min_angle_deg, cfg.limits.max_angle_deg = 0.0, 360.0

    print("home")
    brain.home(0)
    assert wait_idle(brain)
    st = brain.status()
    assert st.homed[0] and abs(st.device_deg[0]) < 0.01, st
    print("  homed")

    print("zero here at 30 deg")
    brain.move_abs(0, 30.0)
    assert wait_idle(brain)
    brain.set_zero(0)
    time.sleep(0.15)
    assert abs(brain.status().angle_deg[0]) < 0.01 or abs(brain.status().angle_deg[0] - 360) < 0.01

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
