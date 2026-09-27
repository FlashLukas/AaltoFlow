"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, set drive params, move in both
STEP and MICROMETRE languages, check the calibration bridge, the datum + display
zero, and the position list.  Prints a short report and exits non-zero if
anything looks wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kim.config import Config  # noqa: E402
from kim.sim_system import build_sim_system  # noqa: E402


def _settle(brain, axis, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def main() -> int:
    cfg = Config()
    cfg.calibration.um_per_step_x = 0.02  # 20 nm/step
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()
    brain.set_step_rate(0, 2000)  # fast so the smoke test settles quickly

    print("STEP language: move X -> 5000 steps")
    brain.move_to_step(0, 5000)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 5000, brain.status().position_steps[0]
    print(f"  X = {brain.status().position_steps[0]} steps "
          f"({brain.status().position_um[0]:.3f} um)")

    print("MICROMETRE language: move X to 120 um (calib 0.02 um/step -> 6000 steps)")
    brain.move_to_um(0, 120.0)
    _settle(brain, 0)
    st = brain.status()
    assert st.position_steps[0] == 6000, st.position_steps[0]
    assert abs(st.position_um[0] - 120.0) < 1e-6, st.position_um[0]
    print(f"  X = {st.position_steps[0]} steps = {st.position_um[0]:.3f} um")

    print("relative move: +10 um (should add 500 steps)")
    brain.move_relative_um(0, 10.0)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 6500, brain.status().position_steps[0]
    print(f"  X = {brain.status().position_steps[0]} steps")

    print("velocity in um/s -> step rate via calibration")
    actual = brain.set_velocity_um(0, 20.0)  # 20 um/s / 0.02 = 1000 steps/s
    assert abs(brain.status().step_rate[0] - 1000.0) < 1e-6, brain.status().step_rate[0]
    print(f"  20 um/s -> {brain.status().step_rate[0]:.0f} steps/s (actual {actual:.3f} um/s)")

    print("velocity clamp: 100 um/s would be 5000 steps/s -> clamps to 2000 (=40 um/s)")
    actual = brain.set_velocity_um(0, 100.0)
    assert abs(brain.status().step_rate[0] - 2000.0) < 1e-6, brain.status().step_rate[0]
    assert abs(actual - 40.0) < 1e-6, actual
    print(f"  clamped to {brain.status().step_rate[0]:.0f} steps/s ({actual:.1f} um/s)")

    print("clamp check: move X -> 9,999,999 steps (max 1,250,000)")
    t = brain.move_to_step(0, 9_999_999)
    assert t == cfg.limits.max_steps_x, t
    print(f"  clamped to {t} steps")

    print("datum: zero the counter here")
    brain.set_step_rate(0, 2000)
    brain.zero_counter(0)
    assert brain.status().position_steps[0] == 0, brain.status().position_steps[0]
    print("  counter reset to 0")

    print("display zero + relative read-out")
    brain.move_to_step(0, 1000)
    _settle(brain, 0)
    brain.set_zero(0)
    assert brain.status().rel_steps[0] == 0, brain.status().rel_steps[0]
    brain.move_steps(0, 250)
    _settle(brain, 0)
    st = brain.status()
    assert st.rel_steps[0] == 250, st.rel_steps[0]
    assert st.position_steps[0] == 1250, st.position_steps[0]
    print(f"  abs {st.position_steps[0]} steps, rel {st.rel_steps[0]} steps ({st.rel_um[0]:.3f} um)")

    print("leash: arm ±1000 steps XY / ±500 steps Z around the datum")
    brain.zero_counter(0)
    brain.set_leash(enabled=True, leash_xy=1000, leash_z=500)
    assert brain.move_to_step(0, 99999) == 1000, "X should clamp to +leash"
    assert brain.move_to_step(2, -99999) == -500, "Z should clamp to -leash"
    st = brain.status()
    assert st.leash and st.limit_hi[0] == 1000 and st.limit_lo[2] == -500
    print(f"  X clamped to {st.limit_hi[0]}, Z to [{st.limit_lo[2]}, {st.limit_hi[2]}]")
    brain.set_leash(enabled=False)
    assert brain.status().leash is False
    print("  leash released")

    print("position list store/goto/save/load")
    brain.store_position(0, "spot")
    p = brain.get_positions()[0]
    assert p["used"] and p["name"] == "spot", p
    brain.save_positions("/tmp/_kim_smoke_positions.json")
    brain.clear_position(0)
    brain.load_positions("/tmp/_kim_smoke_positions.json")
    assert brain.get_positions()[0]["used"], "reload failed"
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
