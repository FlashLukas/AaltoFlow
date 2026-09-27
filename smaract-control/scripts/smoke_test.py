"""Instant offline sanity check -- no hardware, no GUI, no network.

    python scripts/smoke_test.py

Exercises the brain against the simulator: start, absolute move refused before
referencing, a small relative step, find reference, absolute move, clamp,
stop, and the stored-position list. Prints a short report and exits non-zero
if anything looks wrong.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smaract.config import Config  # noqa: E402
from smaract.sim_system import build_sim_system  # noqa: E402


def wait_idle(brain, timeout=20.0) -> None:
    time.sleep(0.05)
    t0 = time.monotonic()
    while brain.status().moving and time.monotonic() - t0 < timeout:
        time.sleep(0.02)


def main() -> int:
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: print(f"  [{level}] {msg}")

    print("start()")
    brain.start()
    brain.set_velocity(10.0)
    assert not brain.status().referenced

    print("absolute move before referencing must be refused")
    try:
        brain.move_to(10.0)
        raise AssertionError("absolute move was accepted on an unreferenced axis")
    except RuntimeError:
        print("  refused, as it should be")

    print("relative step of +0.5 mm (allowed unreferenced)")
    p0 = brain.status().position_mm
    brain.move_by(0.5)
    wait_idle(brain)
    p1 = brain.status().position_mm
    assert abs((p1 - p0) - 0.5) < 2e-3, (p0, p1)
    print(f"  {p0:.4f} -> {p1:.4f} mm")

    print("find reference")
    brain.find_reference()
    wait_idle(brain)
    st = brain.status()
    assert st.referenced, st
    print(f"  referenced at {st.position_mm:.4f} mm (absolute)")

    print("move to 20 mm")
    brain.move_to(20.0)
    wait_idle(brain)
    st = brain.status()
    assert st.on_target and abs(st.position_mm - 20.0) < 1e-3, st
    print(f"  at {st.position_mm:.4f} mm, on target")

    print("clamp: move to 999 mm (limit %.0f)" % cfg.limits.max_mm)
    t = brain.move_to(999.0)
    assert t == cfg.limits.max_mm, t
    time.sleep(0.2)
    brain.stop()
    wait_idle(brain)
    print(f"  clamped to {t} mm, stopped at {brain.status().position_mm:.3f} mm")

    print("stored positions")
    brain.store_position(0, "here")
    path = os.path.join(tempfile.gettempdir(), "_smaract_smoke_positions.json")
    brain.save_positions(path)
    brain.clear_position(0)
    brain.load_positions(path)
    assert brain.get_positions()[0]["used"]
    os.remove(path)
    print("  position list OK")

    brain.shutdown()
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
