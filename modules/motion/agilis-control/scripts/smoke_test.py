"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, move in STEP and MICROMETRE
language, the per-direction step size, the amplitude/calibration link, a jog
with its dead-man, the datum + display zero, the leash and the position list.
Prints a short report and exits non-zero if anything looks wrong.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agilis.config import Config  # noqa: E402
from agilis.sim_system import build_sim_system  # noqa: E402


def _settle(brain, axis, timeout=6.0):
    time.sleep(0.1)
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    time.sleep(0.1)          # one more poll so the snapshot is current


def main() -> int:
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0     # a fast simulated controller keeps this short
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()

    print("STEP language: move X -> 2000 steps")
    brain.move_to_step(0, 2000)
    _settle(brain, 0)
    st = brain.status()
    assert st.position_steps[0] == 2000, st.position_steps[0]
    print(f"  X = {st.position_steps[0]} steps ({st.position_um[0]:.3f} um)")

    print("MICROMETRE language: X to 150 um (0.05 um/step -> 3000 steps)")
    brain.move_to_um(0, 150.0)
    _settle(brain, 0)
    st = brain.status()
    assert st.position_steps[0] == 3000, st.position_steps[0]
    assert abs(st.position_um[0] - 150.0) < 1e-9 and st.target_um[0] == 150.0
    print(f"  X = {st.position_steps[0]} steps = {st.position_um[0]:.3f} um")

    print("asymmetric step size: backward 0.04 um/step, move back 40 um")
    brain.set_calibration(0, 0.04, -1)
    brain.move_relative_um(0, -40.0)          # 1000 backward steps
    _settle(brain, 0)
    st = brain.status()
    assert st.position_steps[0] == 2000, st.position_steps[0]
    assert abs(st.position_um[0] - 110.0) < 1e-9, st.position_um[0]
    print(f"  counter {st.position_steps[0]} but estimate {st.position_um[0]:.3f} um "
          f"(3000 fwd x 50 nm - 1000 bwd x 40 nm)")

    print("amplitude change invalidates the step size")
    brain.set_amplitude(0, 30)
    time.sleep(0.1)
    assert brain.status().cal_valid[0] is False
    brain.set_calibration(0, 0.12)            # re-measured at 30
    time.sleep(0.1)
    assert brain.status().cal_valid[0] is True
    print("  cal_valid False after SU 30, True after a new step size")

    print("clamp: X -> 9,999,999 steps")
    t = brain.move_to_step(0, 9_999_999)
    assert t == cfg.limits.max_steps_x, t
    brain.stop(0)
    print(f"  clamped to {t} steps, stopped")

    print("jog with dead-man")
    cfg.motion.jog_timeout_s = 0.3
    brain.jog(1, 4)
    time.sleep(0.15)
    assert brain.status().jogging[1]
    time.sleep(0.5)
    assert not brain.status().jogging[1], "dead-man did not stop the jog"
    print(f"  jogged Y to {brain.status().position_steps[1]} steps, released itself")

    print("datum + display zero")
    brain.zero_counter(0)
    time.sleep(0.1)
    assert brain.status().position_steps[0] == 0
    brain.move_steps(0, 400)
    _settle(brain, 0)
    brain.set_zero(0)
    brain.move_steps(0, 250)
    _settle(brain, 0)
    st = brain.status()
    assert st.rel_steps[0] == 250 and st.position_steps[0] == 650, (st.rel_steps, st.position_steps)
    print(f"  abs {st.position_steps[0]} steps, rel {st.rel_steps[0]} steps")

    print("leash: +/-1000 steps around the datum")
    brain.set_leash(enabled=True, leash_steps=1000)
    assert brain.move_to_step(0, 99999) == 1000
    brain.stop(0)
    brain.set_leash(enabled=False)
    print("  X clamped to 1000, leash released")

    print("position list store/save/load")
    brain.store_position(0, "spot")
    path = Path(tempfile.gettempdir()) / "_agilis_smoke_positions.json"
    brain.save_positions(str(path))
    brain.clear_position(0)
    brain.load_positions(str(path))
    assert brain.get_positions()[0]["used"], "reload failed"
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
