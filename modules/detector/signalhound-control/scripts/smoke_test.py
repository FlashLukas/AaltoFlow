"""A quick offline sanity check -- no hardware, no network.

    uv run scripts/smoke_test.py

It measures the simulated generator (spectrum mode) and the simulated filter
(tracking mode, against a thru) and compares with what the scene says.

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

from signalhound.config import Config
from signalhound.sim_system import build_sim_system


def _wait(sa):
    while sa.status().acquiring:
        sa.step()


def main() -> int:
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False, seed=1)
    sa.start(run=False)
    ok = True

    # 1. spectrum: the generator's tone, at its frequency and level
    sa.acquire(); _wait(sa)
    t = sa.get_trace("sample")
    sc = cfg.scene
    df_kHz = abs(t["peak_Hz"] - sc.tone_Hz) / 1e3
    dl = abs(t["peak_dBm"] - sc.tone_dBm)
    good = df_kHz <= t["bin_Hz"] / 1e3 and dl < 0.5
    ok &= good
    print(f"  tone: peak {t['peak_Hz'] / 1e9:.6f} GHz ({df_kHz:.1f} kHz off) at "
          f"{t['peak_dBm']:.2f} dBm (set {sc.tone_dBm:g}), floor {t['floor_dBm']:.1f} dBm, "
          f"{t['points']} bins  {'ok' if good else 'FAIL'}")

    # 2. tracking: a thru, then the filter; transmission = the filter alone
    sa.set_tg(True)
    sa.set_scene("dut_inserted", False)
    sa.take_reference(); _wait(sa)
    sa.set_scene("dut_inserted", True)
    sa.acquire(); _wait(sa)
    tx = sa.get_trace("sample", "transmission")
    f, y = tx["freqs_Hz"], tx["transmission"]
    at = lambda hz: float(y[np.argmin(np.abs(f - hz))])          # noqa: E731
    centre = at(sc.dut_center_Hz)
    edge = at(sc.dut_center_Hz + sc.dut_bandwidth_Hz / 2)
    good = abs(centre + sc.dut_loss_dB) < 0.3 and abs(edge - centre + 3.0) < 0.5
    ok &= good
    print(f"  filter: {centre:.2f} dB at the centre (loss {sc.dut_loss_dB:g} dB), "
          f"{edge - centre:.2f} dB at the band edge (expect -3)  {'ok' if good else 'FAIL'}")

    sa.shutdown()
    print("smoke test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
