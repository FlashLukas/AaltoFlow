"""Launch the power meter GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

The real meter, in-process (no service):
    uv run scripts/run_gui.py --real

Connect to a running service (same PC or across the lab network):
    uv run scripts/run_gui.py --connect localhost
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm16.config import Config
from pm16.apps.gui import run_app
from pm16.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs PM16 power meter GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the meter here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: open the real meter in this process")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        from pm16.net.client import Pm16Client
        client = Pm16Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'power meter')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from pm16.backends.tlpmx import TLPMXPowerMeter
        from pm16.meter import PowerMeter
        hw = cfg.hardware
        meter = PowerMeter(TLPMXPowerMeter(hw.resource, hw.dll_path, hw.timeout_ms), cfg)
    else:
        from pm16.sim_system import build_sim_system
        meter, _ = build_sim_system(cfg)
    return run_app(meter, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
