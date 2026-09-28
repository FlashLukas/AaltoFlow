"""Run the Signal Hound TG signal-generator service.

    uv run scripts/run_service.py                       # simulated TG, standalone
    uv run scripts/run_service.py --real                # the real TG44A, THROUGH
                                                        # the signalhound service
    uv run scripts/run_service.py --cmd-port 5625 --pub-port 5626

The service exposes the generator over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

--real does NOT open any USB device. The TG44A can only be driven through the
spectrum analyser's handle, which the signalhound service holds, so this service
becomes a CLIENT of it (default localhost 5587/5588, from shsg.ini [hardware] or
from the launcher's AALTOFLOW_ENDPOINTS). Start signalhound first; the launcher
does that by itself (module.toml start_after).

Drive it with:
    uv run scripts/shsg_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from shsg.config import Config
from shsg.endpoints import apply_launcher_endpoints
from shsg.sim_system import build_real_system, build_sim_system
from shsg.net.service import ShsgService, PortInUse
from shsg.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from shsg.hwlock import HardwareBusy

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module. shsg itself claims
# nothing (the signalhound service owns the hardware), so it is kept only so
# this script answers like every other one if that ever changes.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal Hound TG44A signal generator service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real TG44A through the signalhound service; "
                         "default is a standalone simulator")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use shsg.ini in the project folder if one was saved.
    default_ini = os.path.join(os.path.dirname(__file__), "..", "shsg.ini")
    if not args.config and os.path.isfile(default_ini):
        args.config = default_ini
    cfg = Config.load(args.config) if args.config else Config()
    note = apply_launcher_endpoints(cfg)
    if note:
        print(f"shsg service: {note}")

    if args.real:
        gen, _ = build_real_system(cfg)
        hw = cfg.hardware
        print(f"REAL backend -> signalhound service at {hw.owner_host}:"
              f"{hw.owner_cmd_port}/{hw.owner_pub_port}")
    else:
        gen, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware, no signalhound service needed)")

    service = ShsgService(gen, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). One line in the launcher log and a non-zero exit, instead
        # of a deaf service (gotcha #39).
        print(f"shsg service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        print(f"shsg service: not started: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        print(f"shsg service: could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
