"""A quick offline sanity check -- no hardware, no network, no signalhound service.

    uv run scripts/smoke_test.py

Takes a thru reference with the simulated DUT removed, puts the band-pass back
in and checks that the transmission shows the filter where it is.

Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from shsna.config import Config
from shsna.sim_system import build_sim_system


def _wait(sna, n):
    while sna.status().acquiring:
        sna.step()
    return sna.status().acq_id == n and not sna.status().acq_error


def main() -> int:
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz = 800e6, 1200e6
    sna, _ = build_sim_system(cfg, realtime=False, seed=1)
    sna.start(run=False)
    ok = True

    sna.set_sim("dut_inserted", False)                 # the thru
    ok &= _wait(sna, sna.take_reference())
    ref = sna.get_trace("reference")["reference"]
    print(f"  thru: {ref.mean():.2f} dB mean relative to the TG output "
          f"(expect about -{cfg.sim.pad_dB:g} dB pad - ~1 dB cable)")

    sna.set_sim("dut_inserted", True)                  # the band-pass
    ok &= _wait(sna, sna.acquire())
    r = sna.get_result("transmission")
    good = (abs(r["peak_freq_hz"] - cfg.sim.dut_center_Hz) < 5e6
            and abs(r["peak_transmission_db"] + cfg.sim.dut_loss_dB) < 0.5
            and abs(r["bw3_hz"] - cfg.sim.dut_bandwidth_Hz) < 5e6)
    ok &= good
    print(f"  DUT: peak {r['peak_transmission_db']:.2f} dB at {r['peak_freq_hz'] / 1e6:.1f} MHz, "
          f"-3 dB width {r['bw3_hz'] / 1e6:.1f} MHz (set: {-cfg.sim.dut_loss_dB:g} dB, "
          f"{cfg.sim.dut_center_Hz / 1e6:g} MHz, {cfg.sim.dut_bandwidth_Hz / 1e6:g} MHz)  "
          f"{'ok' if good else 'FAIL'}")
    sna.shutdown()
    print("smoke test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
