"""Run the gaussmeter control service.

    uv run scripts/run_service.py                 # simulated meter
    uv run scripts/run_service.py --real          # the real 455 at the configured VISA resource
    uv run scripts/run_service.py --real --resource ASRL3::INSTR
    uv run scripts/run_service.py --config ls455.ini

The service owns the meter and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5615   (REP)
  * status   on tcp://0.0.0.0:5616   (PUB, 10 Hz)

Every field on the wire is in mT. `measured_field_mT` in the status stream is
the live DC field, so another module can subscribe to it as a field source.
Drive it with:
    uv run scripts/ls455_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from ls455.config import Config
from ls455.gaussmeter import Gaussmeter
from ls455.sim_system import build_sim_system
from ls455.net.service import Ls455Service
from ls455.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Lake Shore 455 gaussmeter control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real meter through pyvisa; default is simulated")
    ap.add_argument("--resource", default=None,
                    help="VISA resource, e.g. GPIB0::12::INSTR or ASRL3::INSTR (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from ls455.backends.ls455 import LakeShore455
        hw = cfg.hardware
        if args.resource:
            hw.resource = args.resource
        backend = LakeShore455(hw.resource, baud_rate=hw.baud_rate, timeout_ms=hw.timeout_ms,
                               command_gap_s=hw.command_gap_s, zero_time_s=hw.zero_time_s)
        meter = Gaussmeter(backend, cfg)
        print(f"REAL backend -> {hw.resource}")
    else:
        meter, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    Ls455Service(meter, host=args.host, cmd_port=args.cmd_port,
                 pub_port=args.pub_port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
