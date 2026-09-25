"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, set velocity, a software-ramp
move, the closed-loop/open-loop switch (incl. travel re-clamp), a relative
"zero here" move, and the position list.  Prints a short report and exits
non-zero if anything looks wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from piezo.config import Config  # noqa: E402
from piezo.sim_system import build_sim_system  # noqa: E402


def _settle(brain, axis, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.02)


def main() -> int:
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()

    print("set velocity + software-ramp move X -> 50 um")
    brain.set_velocity(0, 200.0)  # 200 um/s -> ~0.25 s for 50 um
    brain.move_axis(0, 50.0)
    assert brain.status().moving[0], "expected a ramp in progress"
    _settle(brain, 0)
    x = brain.status().position[0]
    assert abs(x - 50.0) < 0.5, x
    print(f"  X settled at {x:.3f} um")

    print("closed-loop clamp: OL travel 200, CL travel 160")
    brain.set_closed_loop(0, False)          # open loop -> 200 um available
    t = brain.move_axis(0, 190.0)
    assert abs(t - 190.0) < 1e-6, t
    _settle(brain, 0)
    brain.set_closed_loop(0, True)           # closed loop -> re-clamp to 160
    _settle(brain, 0)
    assert abs(brain.status().target[0] - 160.0) < 1e-6, brain.status().target[0]
    print(f"  after CL switch target re-clamped to {brain.status().target[0]:.1f} um")

    print("open-loop vs closed-loop read-out differ (hysteresis model)")
    brain.set_velocity(0, 5000.0)  # fast so we don't wait
    brain.set_closed_loop(0, True)
    brain.move_axis(0, 100.0)
    _settle(brain, 0)
    cl_read = brain.status().position[0]
    brain.set_closed_loop(0, False)
    brain.move_axis(0, 100.0)
    _settle(brain, 0)
    ol_read = brain.status().position[0]
    assert abs(cl_read - 100.0) < 0.05, cl_read
    assert abs(ol_read - 100.0) > 0.1, ol_read
    print(f"  CL reads {cl_read:.3f} um (accurate), OL reads {ol_read:.3f} um (biased)")

    print("relative 'zero here' + relative move")
    brain.set_closed_loop(0, True)
    brain.set_velocity(0, 5000.0)
    brain.move_axis(0, 40.0)
    _settle(brain, 0)
    brain.set_zero(0)
    assert abs(brain.status().relative[0]) < 0.05
    brain.move_relative(0, 10.0)             # -> device 50
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 50.0) < 0.2, brain.status().position[0]
    print("  relative move OK")

    print("position list store/goto/save/load")
    brain.set_velocity(1, 5000.0)
    brain.move_xy(20.0, 30.0)
    _settle(brain, 0); _settle(brain, 1)
    brain.store_position(0, "spot")
    p = brain.get_positions()[0]
    assert p["used"] and p["name"] == "spot", p
    brain.save_positions("/tmp/_piezo_smoke_positions.json")
    brain.clear_position(0)
    brain.load_positions("/tmp/_piezo_smoke_positions.json")
    assert brain.get_positions()[0]["used"], "reload failed"
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
