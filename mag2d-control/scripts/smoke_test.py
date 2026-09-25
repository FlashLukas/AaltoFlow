"""Offline smoke test: the simulated vector magnet, end to end, in a few seconds.

    uv run scripts/smoke_test.py

Runs the controller on SIMULATED TIME (a fake clock), so the several seconds a
real 150 mT step takes pass in milliseconds. Prints ASCII only (suite gotcha
#14: a launcher pipe cannot print arrows or check marks on Windows).
"""

from __future__ import annotations

import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from mag2d.backends.sim import FakeClock
from mag2d.config import Config
from mag2d.controller import Refused
from mag2d.net.describe import build_manifest
from mag2d.sim_system import build_sim_system


def banner(text: str) -> None:
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def run_until_stable(ctrl, clock, limit_s=20.0):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().field_stable:
            return clock() - t0
    return None


def main() -> int:
    banner("1. Config save / load round-trip")
    cfg = Config()
    cfg.interlock.water_bypass = True
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mag2d.ini")
        cfg.save(path)
        back = Config.load(path)
    print(f"   kp {back.control.kp_V_per_mT}, ki {back.control.ki_V_per_mT_s}, "
          f"water_bypass {back.interlock.water_bypass}")
    assert back.interlock.water_bypass is True and back.control.tolerance_mT == cfg.control.tolerance_mT

    banner("2. Settle 150 mT at 45 deg on the simulated magnet")
    cfg = Config()
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=1)
    ctrl.start(run_thread=False)
    ctrl.set_field(150.0, 45.0)
    t = run_until_stable(ctrl, clock)
    s = ctrl.status()
    print(f"   stable after {t:.2f} s (simulated): |B| {s.measured_magnitude_mT:.2f} mT at "
          f"{s.measured_angle_deg:.2f} deg, error {s.error_mT:.3f} mT")
    assert t is not None and s.error_mT < 1.0

    banner("3. Rotate to 135 deg (magnitude kept)")
    ctrl.set_angle(135.0)
    t = run_until_stable(ctrl, clock)
    s = ctrl.status()
    print(f"   stable after {t:.2f} s: setpoint |B| {s.setpoint_field_mT:g} mT, "
          f"measured {s.measured_magnitude_mT:.2f} mT at {s.measured_angle_deg:.2f} deg")
    assert t is not None

    banner("4. Water lost -> FAULT, ramp down, refusals")
    sim.p.water_ok = False
    for _ in range(int(8 * cfg.control.loop_hz)):
        clock.advance(1.0 / cfg.control.loop_hz)
        ctrl.tick()
    s = ctrl.status()
    print(f"   state {s.state}, energized {s.energized}, drive {s.output_V}, fault: {s.fault}")
    assert s.state == "FAULT" and not s.energized and s.output_V == [0.0, 0.0]
    try:
        ctrl.set_field(10.0)
        raise AssertionError("set_field was accepted during a FAULT")
    except Refused as exc:
        print(f"   set_field refused: {exc}")
    sim.p.water_ok = True
    clock.advance(0.02); ctrl.tick()
    ctrl.clear_fault()
    print(f"   water back, fault cleared -> state {ctrl.status().state}")

    banner("5. describe")
    m = build_manifest(ctrl)
    ids = [p["id"] for p in m["parameters"]]
    print(f"   module {m['module']}, {len(ids)} parameters: {', '.join(ids)}")

    ctrl.shutdown()
    banner("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
