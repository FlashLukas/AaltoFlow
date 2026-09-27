"""Run the Keithley 2450 SourceMeter control service.

    uv run scripts/run_service.py                       # simulated 2450 + pretend sample
    uv run scripts/run_service.py --real                # real 2450 over VISA (SCPI)
    uv run scripts/run_service.py --real --visa "USB0::0x05E6::0x2450::<serial>::INSTR"
    uv run scripts/run_service.py --cmd-port 5623 --pub-port 5624

The service owns the instrument and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)
Start-up only READS the 2450 and adopts its state (the output is left as it
was found); the output is switched off on shutdown.
Drive it with:
    uv run scripts/k2450_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from k2450.config import Config
from k2450.smu import SourceMeter
from k2450.sim_system import build_sim_system
from k2450.net.service import K2450Service
from k2450.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Keithley 2450 SourceMeter control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real 2450 over VISA (needs the 'real' extra); default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the 2450 (default: hardware.visa_resource in the config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from k2450.backends.scpi_2450 import VisaK2450
        hw = cfg.hardware
        resource = args.visa or hw.visa_resource
        backend = VisaK2450(resource, visa_library=hw.visa_library,
                            timeout_ms=hw.visa_timeout_ms, terminals=hw.terminals)
        smu = SourceMeter(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        smu, _ = build_sim_system(cfg)
        print(f"SIMULATED backend (no hardware needed), pretend sample: {cfg.sim.load}")

    service = K2450Service(smu, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
