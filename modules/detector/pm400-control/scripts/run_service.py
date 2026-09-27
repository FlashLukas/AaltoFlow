"""Run the PM400 power / energy meter control service.

    uv run scripts/run_service.py                 # simulated console (head from config [sim])
    uv run scripts/run_service.py --real          # the real PM400 (first one found)
    uv run scripts/run_service.py --real --resource USB0::0x1313::0x807D::000000000::INSTR
    uv run scripts/run_service.py --config pm400.ini

The service owns the console and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5617   (REP)
  * status   on tcp://0.0.0.0:5618   (PUB, 10 Hz)

Close Thorlabs OPM first: while it runs it holds the console.
Drive it with:
    uv run scripts/pm400_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm400.config import Config
from pm400.meter import Pm400Meter
from pm400.sim_system import build_sim_system
from pm400.net.service import Pm400Service
from pm400.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs PM400 power/energy meter control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real PM400 through Thorlabs TLPMX; default is simulated")
    ap.add_argument("--resource", default=None,
                    help="TLPMX resource name (default: from config, else the first meter found)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from pm400.backends.tlpmx import TLPMXConsole
        hw = cfg.hardware
        if args.resource:
            hw.resource = args.resource
        backend = TLPMXConsole(hw.resource, dll_path=hw.dll_path,
                               timeout_ms=hw.timeout_ms, channel=hw.channel)
        meter = Pm400Meter(backend, cfg)
        print(f"REAL backend -> {hw.resource or 'first Thorlabs power meter found'}")
    else:
        meter, _ = build_sim_system(cfg)
        print(f"SIMULATED backend (no hardware needed), head: {cfg.sim.head}")

    Pm400Service(meter, host=args.host, cmd_port=args.cmd_port,
                pub_port=args.pub_port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
