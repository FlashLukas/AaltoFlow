"""Offline smoke test: the calibrated vector magnet, end to end, in a few seconds.

    uv run scripts/smoke_test.py

Runs the controller on SIMULATED TIME (a fake clock), so the minute a real
calibration sweep takes passes in milliseconds. Prints ASCII only (suite gotcha
#14: a launcher pipe cannot print arrows or check marks on Windows).

It walks the whole story of this module: measure the magnet, use the measurement,
freeze, and show what happens when you do not.
"""

from __future__ import annotations

import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from mag2dcal.backends.sim import FakeClock
from mag2dcal.calibration import Calibration
from mag2dcal.config import Config
from mag2dcal.controller import Refused
from mag2dcal.net.describe import build_manifest
from mag2dcal.sim_system import build_sim_system


def banner(text: str) -> None:
    print("\n" + "=" * 62)
    print(text)
    print("=" * 62)


def step(ctrl, clock, seconds):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    for _ in range(int(round(seconds / dt))):
        clock.advance(dt)
        ctrl.tick()


def run_until_stable(ctrl, clock, limit_s=30.0):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().field_stable:
            return clock() - t0
    return None


def run_until_calibrated(ctrl, clock, limit_s=600.0):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().state != "CALIBRATE":
            return clock() - t0
    return None


def main() -> int:
    tmp = tempfile.TemporaryDirectory()

    banner("1. Config save / load round-trip")
    cfg = Config()
    cfg.interlock.water_bypass = True
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mag2dcal.ini")
        cfg.save(path)
        back = Config.load(path)
    print(f"   kp {back.control.kp_V_per_mT}, freeze {back.control.freeze_enabled}, "
          f"water_bypass {back.interlock.water_bypass}, "
          f"stabilizer {back.stabilizer.enabled}")
    assert back.interlock.water_bypass is True
    assert back.control.freeze_enabled is True
    assert back.control.tolerance_mT == cfg.control.tolerance_mT

    banner("2. Measure the magnet (both axes, both hysteresis legs)")
    cfg = Config()
    cfg.calibration.directory = tmp.name
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=1)
    ctrl.start(run_thread=False)
    step(ctrl, clock, 0.5)
    ctrl.calibrate(n_per_leg=11, dwell_s=0.5, v_max=5.0)
    t = run_until_calibrated(ctrl, clock)
    cal = ctrl.get_calibration()
    axis = cal.axes[0]
    gap = axis.field_for_volts(0.0, "up") - axis.field_for_volts(0.0, "down")
    print(f"   swept in {t:.0f} s (simulated): {cal.summary()}")
    print(f"   X leg separation at 0 V: {gap:.3f} mT  "
          f"(the simulated magnet's 2h is {2 * cfg.sim.hysteresis_mT:g})")
    print(f"   field envelope now {ctrl.field_envelope_mT():.1f} mT "
          f"(configured limit {cfg.limits.field_max_mT:g})")
    print(f"   auto-saved: {os.listdir(tmp.name)}")
    assert ctrl.is_calibrated and gap > 0.5 * 2 * cfg.sim.hysteresis_mT

    banner("3. Save / load the calibration through a file")
    path = os.path.join(tmp.name, "roundtrip.json")
    cal.save(path)
    back = Calibration.load(path)
    assert back.to_dict() == cal.to_dict()
    print(f"   {back.n_points} points survived the round trip unchanged")

    banner("4. Seek 60 mT at 20 deg on the measured curve")
    ctrl.set_field(60.0, 20.0)
    t = run_until_stable(ctrl, clock)
    s = ctrl.status()
    print(f"   stable after {t:.2f} s: |B| {s.measured_magnitude_mT:.3f} mT at "
          f"{s.measured_angle_deg:.2f} deg, error {s.error_mT:.3f} mT, "
          f"output frozen: {s.frozen}")
    assert t is not None and s.frozen and s.error_mT < cfg.control.tolerance_mT

    banner("5. Why the freeze exists")
    print("   Same plant, same target, 10 s of holding. The stabilizer is off in")
    print("   both, so this compares only the fast loop.")
    for freeze in (True, False):
        c2 = Config()
        c2.control.freeze_enabled = freeze
        c2.stabilizer.enabled = False
        c2.calibration.load_newest_on_start = False
        ck = FakeClock()
        c, sm = build_sim_system(c2, clock=ck, sleep=ck.sleep, seed=1)
        c.start(run_thread=False)
        c.set_field(40.0, 0.0)
        run_until_stable(c, ck)
        travel, worst, crossings = 0.0, 0.0, 0
        prev, last_sign = sm.ao[0], 0
        dt = 1.0 / c2.control.loop_hz
        for _ in range(int(10.0 / dt)):
            ck.advance(dt)
            c.tick()
            travel += abs(sm.ao[0] - prev)
            prev = sm.ao[0]
            err = c.status().setpoint_bx_mT - sm.true_field()[0]
            worst = max(worst, abs(err))
            sign = 1 if err > 0 else -1
            crossings += 1 if last_sign and sign != last_sign else 0
            last_sign = sign
        print(f"   freeze {str(freeze):5}: the X drive moved {travel:6.3f} V, the field "
              f"crossed the setpoint {crossings:3d} times, worst error {worst:.3f} mT")

    banner("6. Water lost -> FAULT, ramp down, refusals")
    sim.p.water_ok = False
    step(ctrl, clock, 8.0)
    s = ctrl.status()
    print(f"   state {s.state}, energized {s.energized}, drive {s.output_V}, "
          f"fault: {s.fault}")
    assert s.state == "FAULT" and not s.energized and s.output_V == [0.0, 0.0]
    try:
        ctrl.set_field(10.0)
        raise AssertionError("set_field was accepted during a FAULT")
    except Refused as exc:
        print(f"   set_field refused: {exc}")
    sim.p.water_ok = True
    step(ctrl, clock, 0.02)
    ctrl.clear_fault()
    print(f"   water back, fault cleared -> state {ctrl.status().state}")

    banner("7. describe")
    m = build_manifest(ctrl)
    ids = [p["id"] for p in m["parameters"]]
    print(f"   module {m['module']}, {len(ids)} parameters: {', '.join(ids)}")

    ctrl.shutdown()
    tmp.cleanup()
    banner("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
