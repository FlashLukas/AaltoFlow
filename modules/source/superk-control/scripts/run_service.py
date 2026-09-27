"""Run the SuperK control service.

    uv run scripts/run_service.py                       # simulated laser
    uv run scripts/run_service.py --real                # the real SuperK (NKT SDK)
    uv run scripts/run_service.py --real --port COM5
    uv run scripts/run_service.py --cmd-port 5611 --pub-port 5612

The service owns the laser and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

CLASS 4 LASER: starting the service never switches emission on. Stopping it
(Ctrl-C, the launcher's Stop, the `shutdown` verb) switches RF and emission off.

Drive it with:
    uv run scripts/superk_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from superk.config import Config
from superk.hwlock import HardwareBusy
from superk.laser import SuperK
from superk.sim_system import build_sim_system
from superk.net.service import SuperkService
from superk.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# superk.ini next to the project is loaded automatically if it exists, so the
# lab PC keeps its port / filter table without passing --config every time.
_DEFAULT_INI = os.path.join(os.path.dirname(__file__), "..", "superk.ini")


def main() -> int:
    ap = argparse.ArgumentParser(description="SuperK supercontinuum laser control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real SuperK through NKTPDLL.dll; default is simulated")
    ap.add_argument("--port", default=None,
                    help="COM port of the real laser (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    ini = args.config or (_DEFAULT_INI if os.path.exists(_DEFAULT_INI) else None)
    cfg = Config.load(ini) if ini else Config()

    if args.real:
        from superk.backends.nktp import NktpSuperK
        hw = cfg.hardware
        port = args.port or hw.port
        backend = NktpSuperK(port, extreme_addr=hw.extreme_addr, rf_addr=hw.rf_addr,
                             autodetect=hw.autodetect, dll_path=hw.dll_path)
        laser = SuperK(backend, cfg)
        print(f"REAL backend -> {port}")
    else:
        laser, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = SuperkService(laser, host=args.host, cmd_port=args.cmd_port,
                            pub_port=args.pub_port)
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (another superk, or any module pointed at this COM
        # port) already holds the laser. The claim comes BEFORE the port is
        # opened, so we never talked to the laser: the shutdown that
        # serve_forever runs on the way out sends nothing to it (the brain is
        # not connected, the backend has no open port). One line, no traceback.
        print(f"superk: cannot start: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        # NKTError (DLL missing, openPorts failed) is a RuntimeError: one
        # readable line in the launcher log instead of a traceback.
        print(f"superk: cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
