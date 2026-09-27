"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

Takes a dark with the light off, then acquires dark-subtracted spectra at three
integration times and checks the brightest line is found where it is (Hg
546.07 nm) and grows with the exposure.

Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from ccs200.config import Config
from ccs200.sim_system import build_sim_system


def _wait(spec):
    while spec.status().acquiring:
        spec.step()


def main() -> int:
    cfg = Config()
    cfg.scan.continuous = False
    spec, _ = build_sim_system(cfg, realtime=False, seed=1)
    spec.start(run=False)
    ok = True
    spec.set_dark_subtract(True)
    last = 0.0
    for t_ms in (2.0, 5.0, 10.0):
        spec.set_integration_time(t_ms / 1e3)
        spec.set_light(False)                  # cap the fibre
        spec.take_dark()
        _wait(spec)
        spec.set_light(True)
        n = spec.acquire()
        _wait(spec)
        t = spec.get_trace("sample")
        good = (t["acq_id"] == n and t["dark_applied"] and abs(t["peak_nm"] - 546.07) < 0.3
                and t["peak_intensity"] > last and not t["saturated"])
        last = t["peak_intensity"]
        ok &= good
        print(f"  {t_ms:5.1f} ms: peak {t['peak_nm']:.3f} nm, {t['peak_intensity']:.4f} FS, "
              f"integrated {t['integrated']:.3f} FS nm  {'ok' if good else 'FAIL'}")
    spec.shutdown()
    print("smoke test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
