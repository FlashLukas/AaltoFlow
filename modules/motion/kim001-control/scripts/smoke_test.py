"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, set drive params, move the
focus axis (Z) in both STEP and MICROMETRE languages, check the calibration
bridge, the datum + display zero, the leash, the X / Y refusal and the
position list.  Prints a short report and exits non-zero if anything looks
wrong.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kim001.config import Config, NotFitted  # noqa: E402
from kim001.sim_system import build_sim_system  # noqa: E402

Z = 2      # the one axis of a KIM001 (index 2 in kim's [X, Y, Z] shape)


def _settle(brain, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[Z] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def main() -> int:
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.02  # 20 nm/step
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()
    brain.set_step_rate(Z, 2000)  # fast so the smoke test settles quickly

    print("STEP language: move Z -> 5000 steps")
    brain.move_to_step(Z, 5000)
    _settle(brain)
    assert brain.status().position_steps[Z] == 5000, brain.status().position_steps[Z]
    print(f"  Z = {brain.status().position_steps[Z]} steps "
          f"({brain.status().position_um[Z]:.3f} um)")

    print("MICROMETRE language: move Z to 120 um (calib 0.02 um/step -> 6000 steps)")
    brain.move_to_um(Z, 120.0)
    _settle(brain)
    st = brain.status()
    assert st.position_steps[Z] == 6000, st.position_steps[Z]
    assert abs(st.position_um[Z] - 120.0) < 1e-6, st.position_um[Z]
    print(f"  Z = {st.position_steps[Z]} steps = {st.position_um[Z]:.3f} um")

    print("relative move: +10 um (should add 500 steps)")
    brain.move_relative_um(Z, 10.0)
    _settle(brain)
    assert brain.status().position_steps[Z] == 6500, brain.status().position_steps[Z]
    print(f"  Z = {brain.status().position_steps[Z]} steps")

    print("velocity in um/s -> step rate via calibration")
    actual = brain.set_velocity_um(Z, 20.0)  # 20 um/s / 0.02 = 1000 steps/s
    assert abs(brain.status().step_rate[Z] - 1000.0) < 1e-6, brain.status().step_rate[Z]
    print(f"  20 um/s -> {brain.status().step_rate[Z]:.0f} steps/s (actual {actual:.3f} um/s)")

    print("clamp check: move Z -> 9,999,999 steps (max 650,000 = 13 mm)")
    t = brain.move_to_step(Z, 9_999_999)
    assert t == cfg.limits.max_steps_z == 650_000, t
    print(f"  clamped to {t} steps")
    brain.stop_all()

    print("X / Y are not fitted on a KIM001: refused")
    for axis in (0, 1):
        try:
            brain.move_to_step(axis, 10)
        except NotFitted as exc:
            print(f"  refused: {exc}")
        else:
            raise AssertionError("an X/Y move was accepted")
    st = brain.status()
    assert st.position_steps[:2] == [0, 0] and st.moving[:2] == [False, False]
    assert st.limit_lo[:2] == [0, 0] and st.limit_hi[:2] == [0, 0]

    print("datum: zero the counter here")
    brain.zero_counter(Z)
    assert brain.status().position_steps[Z] == 0, brain.status().position_steps[Z]
    print("  counter reset to 0")

    print("display zero + relative read-out")
    brain.move_to_step(Z, 1000)
    _settle(brain)
    brain.set_zero(Z)
    assert brain.status().rel_steps[Z] == 0, brain.status().rel_steps[Z]
    brain.move_steps(Z, 250)
    _settle(brain)
    st = brain.status()
    assert st.rel_steps[Z] == 250, st.rel_steps[Z]
    assert st.position_steps[Z] == 1250, st.position_steps[Z]
    print(f"  abs {st.position_steps[Z]} steps, rel {st.rel_steps[Z]} steps ({st.rel_um[Z]:.3f} um)")

    print("leash: arm +/-500 steps around the datum")
    brain.zero_counter(Z)
    brain.set_leash(enabled=True, leash_z=500)
    assert brain.move_to_step(Z, -99999) == -500, "Z should clamp to -leash"
    st = brain.status()
    assert st.leash and st.limit_lo[Z] == -500 and st.limit_hi[Z] == 500
    print(f"  Z clamped to [{st.limit_lo[Z]}, {st.limit_hi[Z]}]")
    brain.set_leash(enabled=False)
    assert brain.status().leash is False
    print("  leash released")

    print("position list store/goto/save/load")
    brain.store_position(0, "focus")
    p = brain.get_positions()[0]
    assert p["used"] and p["name"] == "focus", p
    # the OS temp folder: "/tmp" does not exist on Windows
    pos_file = str(Path(tempfile.gettempdir()) / "_kim001_smoke_positions.json")
    brain.save_positions(pos_file)
    brain.clear_position(0)
    brain.load_positions(pos_file)
    assert brain.get_positions()[0]["used"], "reload failed"
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
