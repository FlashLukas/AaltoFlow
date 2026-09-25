"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, set params, move, home, the
coordinate transform round-trip, and the position list.  Prints a short report
and exits non-zero if anything looks wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stage.config import Config  # noqa: E402
from stage.sim_system import build_sim_system  # noqa: E402


def main() -> int:
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()

    print("set velocity/accel")
    brain.set_velocity(0, 3.0)
    brain.set_acceleration(0, 5.0)

    print("move X -> 5 mm, wait to settle")
    brain.move_axis(0, 5.0)
    for _ in range(200):
        if not brain.status().moving[0]:
            break
        time.sleep(0.02)
    x = brain.status().position[0]
    assert abs(x - 5.0) < 1e-6, x
    print(f"  X settled at {x:.4f} mm")

    print("clamp check: move X -> 999 (limit 25)")
    t = brain.move_axis(0, 999)
    assert t == cfg.limits.max_x, t
    print(f"  clamped to {t} mm")

    print("transform round-trip (rotate 90°, offset)")
    brain.set_offset(0, 1.0)
    brain.set_matrix(0, -1, 1, 0)
    dev = brain.device_from_logical(2.0, 3.0, 4.0)
    log = brain.logical_from_device(*dev)
    assert all(abs(a - b) < 1e-9 for a, b in zip(log, (2.0, 3.0, 4.0))), (dev, log)
    print(f"  logical(2,3,4) -> device{tuple(round(v,3) for v in dev)} -> logical{tuple(round(v,3) for v in log)}")

    print("position list store/goto/save/load")
    brain.set_matrix(1, 0, 0, 1)
    brain.set_offset(0, 0.0)
    brain.move_axis(1, 2.0)
    time.sleep(0.3)
    brain.store_position(0, "home-ish")
    p = brain.get_positions()[0]
    assert p["used"] and p["name"] == "home-ish", p
    brain.save_positions("/tmp/_stage_smoke_positions.json")
    brain.clear_position(0)
    brain.load_positions("/tmp/_stage_smoke_positions.json")
    assert brain.get_positions()[0]["used"], "reload failed"
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
