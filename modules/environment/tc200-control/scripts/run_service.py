"""Run the TC200 heater control service -- simulated (default) or real (--real).

    uv run scripts/run_service.py                         # simulated heater
    uv run --extra real scripts/run_service.py --real     # the real TC200
    uv run --extra real scripts/run_service.py --real --port COM7

The service owns the heater brain and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)
Default ports 5613 / 5614. Drive it with:
    uv run scripts/tc200_console.py

--real needs the TC200 on USB (a virtual COM port) and the `real` extra
installed:  uv sync --extra gui --extra real
Starting the service never switches the heater or changes its setpoint;
stopping it switches the heater OFF unless hardware.disable_on_shutdown = False.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from tc200.config import Config
from tc200.hwlock import HardwareBusy
from tc200.heater import Heater
from tc200.sim_system import build_sim_system
from tc200.net.service import Tc200Service
from tc200.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs TC200 heater control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real TC200 over its serial port (needs the `real` extra)")
    ap.add_argument("--port", default=None,
                    help="serial port, e.g. COM5 (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use tc200.ini in the project folder if this PC saved one
    # (e.g. the serial port, or a higher setpoint limit).
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "tc200.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"tc200 service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.port is not None:
        cfg.hardware.port = args.port

    if args.real:
        from tc200.backends.serial_tc200 import SerialTC200
        heater = Heater(SerialTC200(cfg), cfg)
        print(f"REAL backend -> TC200 on {cfg.hardware.port}")
    else:
        heater, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = Tc200Service(heater, host=args.host, cmd_port=args.cmd_port,
                           pub_port=args.pub_port)
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (tc200 or any other module) already holds this COM
        # port. We never opened the instrument, so there is nothing to switch
        # off or close: one clean line on stderr and a non-zero exit.
        print(f"tc200 service: could not start: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        # most often: wrong COM port, or the `real` extra not installed
        print(f"could not start: {exc}")
        if args.real:
            print("check: the TC200 is on and plugged in, the COM port is right "
                  "(Device Manager), the extra is installed (uv sync --extra gui --extra real)")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
