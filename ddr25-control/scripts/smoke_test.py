"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, the homing rule, home,
absolute + relative moves, a wrap policy, the clamp, stored angles, stop.
Prints a short report and exits non-zero if anything looks wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddr25.config import Config  # noqa: E402
from ddr25.sim_system import build_sim_system  # noqa: E402


def wait_idle(brain, timeout: float = 20.0) -> None:
    t0 = time.monotonic()
    time.sleep(0.05)
    while brain.status().moving:
        if time.monotonic() - t0 > timeout:
            raise SystemExit("timed out waiting for the stage")
        time.sleep(0.02)


def main() -> int:
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()
    brain.set_velocity(360.0)
    brain.set_acceleration(3000.0)

    print("absolute move before homing must be refused")
    try:
        brain.move_to(10.0)
        raise SystemExit("FAIL: moved without homing")
    except RuntimeError as exc:
        print(f"  refused: {exc}")

    print("home")
    brain.home()
    wait_idle(brain)
    st = brain.status()
    assert st.homed and abs(st.raw_deg) < 0.01, st
    print(f"  homed, controller at {st.raw_deg:.4f} deg")

    print("move to 90 deg (literal)")
    brain.move_to(90.0)
    wait_idle(brain)
    assert abs(brain.status().angle_deg - 90.0) < 0.01
    print(f"  at {brain.status().angle_deg:.4f} deg")

    print("shortest way from 90 to 350 deg must turn -100, not +260")
    brain.set_wrap("shortest")
    brain.move_to(350.0)
    wait_idle(brain)
    st = brain.status()
    assert abs(st.raw_deg - (-10.0)) < 0.01 and abs(st.angle_deg - 350.0) < 0.01, st
    print(f"  controller {st.raw_deg:.4f}, shown {st.angle_deg:.4f}")

    print("clamp in literal mode: 5000 -> max_deg")
    brain.set_wrap("literal")
    t = brain.move_to(5000.0)
    assert t == cfg.limits.max_deg, t
    brain.stop()
    wait_idle(brain)

    print("stored angle round trip")
    brain.move_to(33.0)
    wait_idle(brain)
    brain.store_angle(0, "s-pol")
    brain.move_by(20.0)
    wait_idle(brain)
    brain.goto_angle(0)
    wait_idle(brain)
    assert abs(brain.status().angle_deg - 33.0) < 0.01
    print("  OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
