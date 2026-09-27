"""Run the SR830 lock-in control service.

    uv run scripts/run_service.py                                   # simulated SR830
    uv run scripts/run_service.py --real                            # the real one (GPIB0::8)
    uv run scripts/run_service.py --real --resource GPIB0::12::INSTR
    uv run scripts/run_service.py --config sr830.ini

--real needs `uv sync --extra gui --extra real` and a VISA library with a GPIB
driver (NI-VISA + NI-488.2) on this PC.

The service owns the lock-in and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5599   (REP)
  * status   on tcp://0.0.0.0:5600   (PUB, 10 Hz)

Drive it with:
    uv run scripts/sr830_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from sr830.config import Config
from sr830.lockin import DspLockIn
from sr830.sim_system import build_sim_system
from sr830.net.service import Sr830Service
from sr830.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="SR830 lock-in control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real SR830 over GPIB (needs pyvisa + a VISA library); "
                         "default is simulated")
    ap.add_argument("--resource", default=None,
                    help="VISA resource of the real SR830, e.g. GPIB0::8::INSTR "
                         "(default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from sr830.backends.visa_sr830 import VisaSR830
        hw = cfg.hardware
        if args.resource:
            hw.resource = args.resource
        backend = VisaSR830(hw.resource, timeout_ms=hw.timeout_ms,
                            front_panel_override=hw.front_panel_override)
        lockin = DspLockIn(backend, cfg)
        print(f"REAL backend -> {hw.resource}")
    else:
        lockin, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    Sr830Service(lockin, host=args.host, cmd_port=args.cmd_port,
                 pub_port=args.pub_port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
