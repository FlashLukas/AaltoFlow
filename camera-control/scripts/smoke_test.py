"""Instant offline sanity check for the camera module (no hardware, no GUI).

Builds the simulated closed-loop system, captures a template, runs the stabiliser
onto a chosen scanning point, and runs an autofocus sweep -- printing each result.
Run it after any change for a 3-second confidence check:

    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from camera.config import Config              # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402


def main() -> None:
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.autofocus.drive_amplitude_v = 12.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    time.sleep(0.1)

    tcx, tcy = cam.template_center_px()
    print("template drawn at (%.1f, %.1f)" % (tcx, tcy))
    print("capture:", brain.capture_reference((tcx, tcy, 60, 60)))
    brain.set_tracking(True)
    time.sleep(0.05)
    s = brain.status()
    print("  match=%s score=%.3f  spot=(%.1f, %.1f)  template=(%.1f, %.1f)"
          % (s.match_found, s.match_score, s.spot_x, s.spot_y, s.template_x, s.template_y))

    print("stabilise onto scan point (2, 2) ...")
    brain.set_selected_index(2, 2)
    brain.set_stabilize(True)
    for i in range(120):
        time.sleep(0.02)
        if brain.status().stable:
            break
    s = brain.status()
    print("  stable=%s after ~%d frames  distance=%.3f um  stage=(%.2f, %.2f) um"
          % (s.stable, i, s.distance_um, s.stage_x, s.stage_y))

    print("autofocus (true focus = 7.6 V) ...")
    brain.set_stabilize(False)
    brain.set_z(4.0)
    time.sleep(0.05)
    brain.autofocus()
    for _ in range(300):
        time.sleep(0.02)
        if not brain.status().af_running:
            break
    s = brain.status()
    print("  best focus = %.3f V  (parked at %.3f V)  error=%s"
          % (s.best_focus_v, s.z_voltage, s.af_error))

    brain.shutdown()
    print("smoke test OK")


if __name__ == "__main__":
    main()
