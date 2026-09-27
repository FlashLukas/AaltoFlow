"""Run the SMB100A control service.

    uv run scripts/run_service.py                       # simulated generator
    uv run scripts/run_service.py --real                # real SMB100A over GPIB
    uv run scripts/run_service.py --real --visa GPIB0::28::INSTR
    uv run scripts/run_service.py --cmd-port 5557 --pub-port 5558

The service owns the generator and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

Default ports are 5557 / 5558 so this runs happily next to the magnet service
(5555 / 5556). Drive it with:
    uv run scripts/rf_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from smb.config import Config
from smb.generator import Generator
from smb.sim_system import build_sim_system
from smb.net.service import SmbService
from smb.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="SMB100A RF generator control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real SMB100A over GPIB (needs pyvisa); default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the real SMB100A (default: from config, GPIB0::28::INSTR)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from smb.backends.visa_scpi import VisaSMB100A
        resource = args.visa or cfg.hardware.smb_visa
        backend = VisaSMB100A(resource,
                              timeout_ms=cfg.hardware.visa_timeout_ms,
                              settle_s=cfg.hardware.settle_s)
        gen = Generator(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        gen, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = SmbService(gen, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
