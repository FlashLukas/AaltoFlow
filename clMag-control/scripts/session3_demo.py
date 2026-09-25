"""Session 3 demo: the full controller running headless against the simulator.

Run from the project root:   uv run scripts/session3_demo.py

It builds a calibration, starts the Controller (acquisition thread + control
thread), then drives a little experiment: seek to +50 mT, hold, seek to -20 mT,
demagnetise to zero, and shut down safely. Watch the state column move
IDLE -> RAMPING -> SEEK -> STABLE and the field lock in within tolerance.
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from clMag.config import Config
from clMag.calibration import FieldCalibration
from clMag.backends.sim import SimulatedKepco, SimulatedHallProbe
from clMag.acquisition import AcquisitionThread
from clMag.controller import Controller


def build_calibration(cfg, kepco, probe):
    """Quick synchronous sweep to get a B(I) curve before we go live."""
    probe.emulate_timing = False        # build fast, no acquisition delay
    I_max = cfg.limits.current_max_A
    up = [(-I_max) + 2 * I_max * i / 49 for i in range(50)]
    raw = []
    for I in up + list(reversed(up)):
        kepco.set_current(I)
        v = probe.read_voltage(cfg.acquisition.precise_samples, cfg.acquisition.precise_rate_Hz)
        raw.append((I, cfg.hall.volts_to_field(v)))
    probe.emulate_timing = True         # from here on, realistic timing
    return FieldCalibration.from_sweep(raw, hall=cfg.hall)


def wait_until(ctrl, predicate, timeout_s, label):
    """Poll status until predicate(status) is true, printing the trajectory."""
    t0 = time.monotonic()
    last_state = None
    while time.monotonic() - t0 < timeout_s:
        s = ctrl.status()
        if s.state != last_state:
            print(f"    [{time.monotonic()-t0:5.2f}s] state -> {s.state}")
            last_state = s.state
        if predicate(s):
            print(f"    [{time.monotonic()-t0:5.2f}s] {label}: field={s.measured_field_mT:.3f} mT "
                  f"current={s.current_A:.3f} A stable={s.field_stable}")
            return True
        time.sleep(0.05)
    print(f"    TIMEOUT waiting for {label} (state={ctrl.status().state})")
    return False


def main() -> int:
    cfg = Config()
    # PID gains tuned for the SIMULATED magnet. The config defaults (Kc 0.002,
    # Ti 5) are the starting point carried over from the LabVIEW system and must
    # be re-tuned against the real magnet; these are the sim-tuned values.
    cfg.pid.Kc_A_per_mT = 0.01
    cfg.pid.Ti_s = 0.15

    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall)
    kepco.open()

    print("Building calibration ...")
    cal = build_calibration(cfg, kepco, probe)
    lo, hi = cal.range_mT
    print(f"  {len(cal.currents_A)} points, {lo:.1f}..{hi:.1f} mT\n")

    acq = AcquisitionThread(probe, cfg.hall, cfg.acquisition)
    events = []
    ctrl = Controller(cfg, kepco, acq, calibration=cal,
                      on_event=lambda lvl, msg: (events.append((lvl, msg)),
                                                 print(f"  [{lvl:5}] {msg}")))
    ctrl.start()

    def seek_and_wait(target, timeout=8.0):
        ctrl.set_field(target)
        # wait for the control loop to accept the command (clears the old stable
        # flag) before we start waiting for the NEW stable -- avoids racing on a
        # stale status from the previous setpoint.
        wait_until(ctrl, lambda s: not s.field_stable, 2.0, "command accepted")
        wait_until(ctrl, lambda s: s.field_stable, timeout, "stable")

    try:
        print("\n-- seek to +50 mT --")
        seek_and_wait(50.0)

        print("\n-- hold 1 s, then seek to -20 mT --")
        time.sleep(1.0)
        seek_and_wait(-20.0)

        print("\n-- demagnetise (amplitude 1.5 A) --")
        ctrl.demag(1.5)
        wait_until(ctrl, lambda s: s.state == "IDLE", 20.0, "demag done")
        time.sleep(0.3)     # let a fresh precise reading land
        print(f"    after demag: field={ctrl.status().measured_field_mT:.3f} mT "
              f"current={ctrl.status().current_A:.3f} A")

    finally:
        print("\n-- shutdown --")
        ctrl.shutdown()

    print(f"\nfinal current: {kepco.read_current():.4f} A  (should be 0)")
    print("DEMO COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
