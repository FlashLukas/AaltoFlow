"""Run the Kepco BOP control service.

    uv run scripts/run_service.py                       # simulated BOP + coil
    uv run scripts/run_service.py --real                # the real BOP over GPIB
    uv run scripts/run_service.py --real --visa GPIB0::6::INSTR
    uv run scripts/run_service.py --cmd-port 5581 --pub-port 5582

The service owns the supply and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)

At start the service READS the BOP (mode, setpoint, limit, output on/off) and
adopts it -- nothing is written, so a live output stays live. Every way the
service stops (Ctrl-C, the `shutdown` command, the launcher's Stop) ramps the
output to zero and switches it off first.

This is the SAME physical BOP that clMag-control drives (GPIB0::6::INSTR):
never run the two services at the same time. Drive it with:
    uv run scripts/kepco_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from kepco.config import Config
from kepco.supply import BipolarSupply
from kepco.sim_system import build_sim_system
from kepco.net.service import KepcoService
from kepco.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Kepco BOP bipolar supply control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real BOP over GPIB (needs the `real` extra); "
                         "default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the BOP (default: from config, GPIB0::6::INSTR)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from kepco.backends.bop_gpib import VisaBOP
        resource = args.visa or cfg.hardware.visa
        backend = VisaBOP(resource, timeout_ms=cfg.hardware.visa_timeout_ms,
                          full_range=cfg.hardware.full_range)
        supply = BipolarSupply(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        supply, _ = build_sim_system(cfg)
        print("SIMULATED backend (a BOP driving a coil; no hardware needed)")

    service = KepcoService(supply, host=args.host, cmd_port=args.cmd_port,
                           pub_port=args.pub_port)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
