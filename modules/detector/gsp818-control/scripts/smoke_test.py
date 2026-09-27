"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

It checks the two things the module is for: a carrier shows up where it is,
at its level, and a thru-normalised filter shows its passband loss.
Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

import numpy as np

from gsp818.config import Config
from gsp818.sim_system import build_sim_system


def _acquire(sa):
    n = sa.acquire()
    while sa.status().acquiring:
        sa.step()
    return n


def main() -> int:
    cfg = Config()
    cfg.acquisition.continuous = False
    sa, sim = build_sim_system(cfg, realtime=False, seed=1)
    sa.start(run=False)
    ok = True

    # 1) the 100 MHz / -20 dBm carrier on a 2 MHz span
    sa.set_center(100e6)
    sa.set_span(2e6)
    _acquire(sa)
    t = sa.get_trace("sample")
    good = abs(t["peak_Hz"] - 100e6) < 2 * 2e6 / 600 and abs(t["peak_dBm"] + 20) < 0.5
    ok &= good
    print(f"  carrier: peak {t['peak_Hz'] / 1e6:.4f} MHz at {t['peak_dBm']:.2f} dBm, "
          f"floor {t['floor_dBm']:.1f} dBm  {'ok' if good else 'FAIL'}")

    # 2) scalar network analysis: thru reference, then the 900 MHz bandpass
    sa.set_start(100e6)
    sa.set_stop(1.7e9)
    sa.set_tg(True)
    sa.set_dut("thru")
    sa.take_reference()
    while sa.status().acquiring:
        sa.step()
    sa.set_dut("bandpass")
    _acquire(sa)
    n = sa.get_trace("sample", "norm")
    i = int(np.argmin(abs(n["freqs_Hz"] - cfg.bench.dut_center_Hz)))
    loss = -n["norm_dB"][i]
    stop = -n["norm_dB"][int(np.argmin(abs(n["freqs_Hz"] - 300e6)))]
    good = abs(loss - cfg.bench.dut_loss_dB) < 0.5 and stop > 30
    ok &= good
    print(f"  bandpass: {loss:.2f} dB loss at {cfg.bench.dut_center_Hz / 1e6:.0f} MHz, "
          f"{stop:.1f} dB at 300 MHz  {'ok' if good else 'FAIL'}")

    sa.shutdown()
    good = sim.tg_output is False
    ok &= good
    print(f"  tracking generator off after shutdown  {'ok' if good else 'FAIL'}")
    print("smoke test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
