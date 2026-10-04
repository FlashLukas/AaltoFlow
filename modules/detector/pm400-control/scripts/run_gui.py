"""Launch the PM400 power / energy meter GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

The real console, in-process (no service):
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

from pm400.config import Config
from pm400.apps.gui import run_app
from pm400.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Thorlabs PM400 power/energy meter GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the meter here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: open the real PM400 in this process")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        from pm400.net.client import Pm400Client
        # kind "gui": the service counts this window as a viewer, and the first
        # GUI to connect gets control (control.py / apps/control_bar.py)
        client = Pm400Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                             kind="gui", name="pm400 GUI")
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'PM400')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from pm400.backends.tlpmx import TLPMXConsole
        from pm400.meter import Pm400Meter
        hw = cfg.hardware
        meter = Pm400Meter(TLPMXConsole(hw.resource, hw.dll_path, hw.timeout_ms, hw.channel), cfg)
    else:
        from pm400.sim_system import build_sim_system
        meter, _ = build_sim_system(cfg)
    return run_app(meter, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
