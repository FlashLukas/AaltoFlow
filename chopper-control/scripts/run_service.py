"""Run the optical-chopper control service.

    uv run scripts/run_service.py                       # simulated MC2000B
    uv run scripts/run_service.py --real                # real MC2000B (needs --extra real)
    uv run scripts/run_service.py --real --port COM7
    uv run scripts/run_service.py --cmd-port 5609 --pub-port 5610

The service owns the controller and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

It ADOPTS the chopper's state (a running wheel keeps running) and leaves it as
it is on exit, unless hardware.stop_on_exit is set. Drive it with:
    uv run scripts/chopper_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from chopper.config import Config
from chopper.chopper import Chopper
from chopper.sim_system import build_sim_system
from chopper.net.service import ChopperService
from chopper.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs MC2000B optical chopper service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real MC2000B over USB serial (needs pyserial: "
                         "uv sync --extra gui --extra real); default is simulated")
    ap.add_argument("--port", default=None,
                    help="COM port of the real MC2000B (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from chopper.backends.mc2000b import SerialMC2000B
        port = args.port or cfg.hardware.port
        backend = SerialMC2000B(port, baud=cfg.hardware.baud,
                                timeout_s=cfg.hardware.timeout_s,
                                quiet_on_open=cfg.hardware.quiet_on_open)
        ch = Chopper(backend, cfg, simulated=False)
        print(f"REAL backend -> {port}")
    else:
        ch, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = ChopperService(ch, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
