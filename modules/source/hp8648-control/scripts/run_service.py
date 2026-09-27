"""Run the HP 8648D control service.

    uv run scripts/run_service.py                       # simulated generator
    uv run scripts/run_service.py --real                # real 8648D over GPIB
    uv run scripts/run_service.py --real --visa GPIB0::19::INSTR
    uv run scripts/run_service.py --cmd-port 5619 --pub-port 5620

The service owns the generator and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

At start the service READS the generator and adopts its state (RF, frequency,
level, modulation) without changing anything. The RF output is switched OFF
when the service stops (Ctrl-C, the `shutdown` verb, or the launcher's Stop). Drive it with:
    uv run scripts/hp8648_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from hp8648.config import Config
from hp8648.hwlock import HardwareBusy
from hp8648.source import SignalSource
from hp8648.sim_system import build_sim_system
from hp8648.net.service import Hp8648Service
from hp8648.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="HP 8648D RF generator control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real 8648D over GPIB (needs the `real` extra); default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the real 8648D (default: from config, GPIB0::19::INSTR)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from hp8648.backends.visa_8648 import Visa8648
        resource = args.visa or cfg.hardware.visa_resource
        backend = Visa8648(resource, timeout_ms=cfg.hardware.visa_timeout_ms)
        src = SignalSource(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        src, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = Hp8648Service(src, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (a second hp8648, or any module pointed at this GPIB
        # address) already drives the generator. We never opened it, so there
        # is nothing to switch off: say who holds it, in one line, and exit.
        print(f"hp8648: cannot start: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        # A failed open (no VISA, nothing at the address, timeout) has already
        # released the address and closed the session inside the backend. One
        # readable line in the launcher log instead of a traceback.
        print(f"hp8648: cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
