"""Run the power meter control service.

    uv run scripts/run_service.py                 # simulated meter
    uv run scripts/run_service.py --real          # the real PM16 (first one found)
    uv run scripts/run_service.py --real --resource USB0::0x1313::0x807B::000000000::INSTR
    uv run scripts/run_service.py --config pm16.ini

The service owns the meter and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5571   (REP)
  * status   on tcp://0.0.0.0:5572   (PUB, 10 Hz)

Close Thorlabs OPM first: while it runs it holds the meter.
Drive it with:
    uv run scripts/pm16_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm16.config import Config
from pm16.hwlock import HardwareBusy
from pm16.meter import PowerMeter
from pm16.sim_system import build_sim_system
from pm16.net.service import Pm16Service, PortInUse
from pm16.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs PM16 power meter control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real meter through Thorlabs TLPMX; default is simulated")
    ap.add_argument("--resource", default=None,
                    help="TLPMX resource name (default: from config, else the first meter found)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from pm16.backends.tlpmx import TLPMXPowerMeter
        hw = cfg.hardware
        if args.resource:
            hw.resource = args.resource
        backend = TLPMXPowerMeter(hw.resource, dll_path=hw.dll_path, timeout_ms=hw.timeout_ms)
        meter = PowerMeter(backend, cfg)
        print(f"REAL backend -> {hw.resource or 'first Thorlabs power meter found'}")
    else:
        meter, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = Pm16Service(meter, host=args.host, cmd_port=args.cmd_port,
                          pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"pm16 service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (another pm16, or any module pointed at this meter)
        # already holds its USB address. We never opened the meter, so there
        # is nothing to close: say who holds it, in one line, and exit.
        print(f"pm16: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except RuntimeError as exc:
        # TLPMXError (DLL missing, no meter, I/O error) is a RuntimeError:
        # one readable line in the launcher log instead of a traceback.
        print(f"pm16: cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
