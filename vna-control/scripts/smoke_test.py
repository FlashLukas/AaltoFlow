"""A quick offline sanity check -- no hardware, no network, no magnet.

    uv run scripts/smoke_test.py

Output is ASCII only: mission-control captures stdout through a pipe, where
anything outside cp1252 raises (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from vna import model
from vna.config import Config
from vna.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    cfg.field.source = "manual"
    vna, _ = build_sim_system(cfg, realtime=False, seed=1)
    vna.start(run=False)
    ok = True
    # a reference where the line is out of the band, as a VNA-FMR run starts
    vna.set_manual_field(0.0)
    vna.take_reference()
    while vna.status().acquiring:
        vna.step()
    print(f"  reference #{vna.status().reference['acq_id']} at 0 mT")
    for field in (20.0, 50.0, 90.0):
        vna.set_manual_field(field)
        n = vna.acquire()
        while vna.status().acquiring:
            vna.step()
        t = vna.get_trace("sample")
        u = vna.get_trace("sample", "u")
        expect = model.kittel_Hz(field, cfg.sample)
        err_MHz = abs(t["dip_Hz"] - expect) / 1e6
        # divided by the reference, the deepest point of |1 + u| is the line
        u_dip = u["freqs_Hz"][abs(1 + u["u"]).argmin()]
        u_err_MHz = abs(u_dip - expect) / 1e6
        good = t["acq_id"] == n and err_MHz < 2.0 and u_err_MHz < 5.0
        ok &= good
        print(f"  {field:5.1f} mT: dip {t['dip_Hz'] / 1e9:.4f} GHz ({t['dip_dB']:.2f} dB), "
              f"Kittel {expect / 1e9:.4f} GHz, error {err_MHz:.2f} MHz, "
              f"u error {u_err_MHz:.2f} MHz  {'ok' if good else 'FAIL'}")
    vna.shutdown()
    print("smoke test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
