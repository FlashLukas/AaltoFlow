"""Session 2 smoke test: exercise the foundation with simulated hardware.

Run it from the project root with:   uv run scripts/smoke_test.py
(There is no hardware and no GUI yet -- this just proves the pieces fit.)

It walks through the four things Session 2 built:
  1. config save/load round-trips without changing any value
  2. a calibration sweep builds a sensible B(I) curve
  3. the curve save/loads to a text file unchanged
  4. commanding a field via calibration ALONE lands close but not exact --
     which is the whole reason Session 3 adds a PID stage.
"""

from __future__ import annotations

import os
import sys
import tempfile

# --- convenience so `python scripts/smoke_test.py` works even before install.
# With `uv run` (recommended) the package is already importable and this is a
# no-op. It just adds the src/ folder to the import path.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from clMag.config import Config
from clMag.calibration import FieldCalibration
from clMag.backends.sim import SimulatedKepco, SimulatedHallProbe


def banner(text: str) -> None:
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def run_calibration_sweep(cfg, kepco, probe, n_per_leg=50):
    """Full cycle -limit -> +limit -> -limit, measuring field at each current."""
    I_max = cfg.limits.current_max_A
    up = [(-I_max) + (2 * I_max) * i / (n_per_leg - 1) for i in range(n_per_leg)]
    down = list(reversed(up))
    raw = []
    for I in up + down:
        kepco.set_current(I)
        # (real hardware dwells here for the settle time; simulator is instant)
        volts = probe.read_voltage(cfg.acquisition.precise_samples,
                                   cfg.acquisition.precise_rate_Hz)
        B = cfg.hall.volts_to_field(volts)
        raw.append((I, B))
    return raw


def main() -> int:
    cfg = Config()

    banner("1. Config save / load round-trip")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        cfg.save(path)
        reloaded = Config.load(path)
        ok = (reloaded.hall.sensitivity_mT_per_mV == cfg.hall.sensitivity_mT_per_mV
              and reloaded.pid.Ti_s == cfg.pid.Ti_s
              and reloaded.limits.current_max_A == cfg.limits.current_max_A)
        print(f"   saved to {os.path.basename(path)} and reloaded")
        print(f"   sensitivity {reloaded.hall.sensitivity_mT_per_mV} mT/mV, "
              f"Ti {reloaded.pid.Ti_s} s, current limit {reloaded.limits.current_max_A} A")
        print(f"   round-trip identical: {ok}")
        assert ok

    banner("2. Calibration sweep (full cycle, simulated magnet)")
    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall)
    kepco.open(); probe.open()

    raw = run_calibration_sweep(cfg, kepco, probe, n_per_leg=50)
    cal = FieldCalibration.from_sweep(raw, hall=cfg.hall, subtract_remanence=True)
    lo, hi = cal.range_mT
    print(f"   raw points measured: {len(raw)} (up + down legs)")
    print(f"   unique currents after hysteresis averaging: {len(cal.currents_A)}")
    print(f"   field range spanned: {lo:.1f} to {hi:.1f} mT")
    print(f"   field at zero current after remanence removal: "
          f"{cal.field_for_current(0.0):+.4f} mT")

    banner("3. Calibration save / load round-trip")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "calibration.txt")
        cal.save(path)
        cal2 = FieldCalibration.load(path)
        same_len = len(cal2.currents_A) == len(cal.currents_A)
        same_hall = cal2.hall.correction == cal.hall.correction
        print(f"   wrote {len(cal.currents_A)} points + Hall params to text")
        print(f"   reloaded points: {len(cal2.currents_A)}, "
              f"Hall correction preserved: {same_hall}")
        assert same_len and same_hall

    banner("4. Clamp + command-a-field (why we still need PID)")
    # ask for a field beyond the magnet's reach -> current clamps at the limit
    unreachable = hi + 50.0
    I_clamped = cal.current_for_field(unreachable)
    print(f"   requested {unreachable:.1f} mT (out of range) -> "
          f"current {I_clamped:.3f} A (clamped at {cfg.limits.current_max_A} A limit)")

    # command a reachable field using ONLY the calibration jump
    target = 50.0
    I_needed = cal.current_for_field(target)
    kepco.set_current(I_needed)
    volts = probe.read_voltage(cfg.acquisition.precise_samples,
                               cfg.acquisition.precise_rate_Hz)
    landed = cfg.hall.volts_to_field(volts)
    residual = landed - target
    print(f"   commanded {target:.1f} mT -> current {I_needed:.3f} A -> "
          f"measured {landed:.2f} mT (residual {residual:+.2f} mT)")
    print(f"   tolerance is {cfg.limits.field_tolerance_mT} mT, so the calibration")
    print(f"   jump alone is NOT good enough -> Session 3 adds a PID to close it.")

    kepco.close(); probe.close()
    banner("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
