"""Run the NI USB-6001 DAQ service.

    uv run scripts/run_service.py                       # simulated card
    uv run scripts/run_service.py --real                # the real card (needs nidaqmx)
    uv run scripts/run_service.py --real --device Dev2
    uv run scripts/run_service.py --cmd-port 5629 --pub-port 5630

The service owns the card and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

CONFIG: without --config it loads `usb6001.ini` from this project folder if the
file exists (it is not in git: it is this PC's wiring). Settings > Apply in the
GUI saves back into it, so a change of the digital line DIRECTIONS survives to
the next start -- which is when it is applied. To change a direction: edit it
in Settings (or the .ini), then restart this service.

Exit codes: 0 clean stop, 2 could not start (port taken, card missing, ...),
4 the card is already in use by another service (the same number in every
AaltoFlow module, see hwlock.py).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from usb6001.config import Config
from usb6001.daq import Daq
from usb6001.hwlock import HardwareBusy
from usb6001.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from usb6001.net.service import PortInUse, Usb6001Service
from usb6001.sim_system import build_sim_system

EXIT_HARDWARE_BUSY = 4
DEFAULT_INI = Path(__file__).resolve().parents[1] / "usb6001.ini"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NI USB-6001 DAQ control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real USB-6001 through nidaqmx; default is simulated")
    ap.add_argument("--device", default=None,
                    help="DAQmx device name from NI MAX (default: from config, Dev1)")
    ap.add_argument("--config", default=None,
                    help=f"a .ini config (default: {DEFAULT_INI.name} in the project folder, if present)")
    args = ap.parse_args(argv)

    path = Path(args.config) if args.config else DEFAULT_INI
    cfg = Config.load(str(path)) if path.exists() else Config()
    if args.device:
        cfg.hardware.device = args.device
    real = args.real or cfg.hardware.driver.strip().lower() == "nidaq"

    if real:
        from usb6001.backends.nidaq import NidaqUsb6001
        cfg.hardware.driver = "nidaq"
        daq = Daq(NidaqUsb6001(cfg.hardware), cfg)
        print(f"REAL backend -> NI-DAQmx device {cfg.hardware.device}")
    else:
        daq, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")
    # Apply saves here, so a layout change is there at the next start.
    daq.config_path = str(path)
    print(f"config: {path}" + ("" if path.exists() else " (not there yet: defaults)"))

    service = Usb6001Service(daq, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # Nothing was opened: the sockets are bound BEFORE the card (gotcha #39).
        print(f"usb6001 service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service already drives this card (hwlock: one physical card,
        # one service). open() stopped at the claim, before the first task, so
        # nothing is written and nothing of the other service's is closed.
        print(f"usb6001 service: not started: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        print(f"usb6001 service: could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        if real:
            print("  check: the USB-6001 is plugged in, its name in NI MAX matches "
                  "hardware.device, and nidaqmx is installed "
                  "(uv sync --extra gui --extra real)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
