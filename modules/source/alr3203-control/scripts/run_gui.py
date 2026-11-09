"""Launch the ALR3203 magnet-supply GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py
    uv run scripts/run_gui.py --demo          # sim supply found live at 2.5 A (adopted)

Connect to a running service (same PC or across the lab network):
    uv run scripts/run_gui.py --connect localhost
    uv run scripts/run_gui.py --connect 192.168.1.42 --cmd-port 5635 --pub-port 5636
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from alr3203.apps.gui import main as run_local, run_app
from alr3203.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="ELC ALR3203 magnet supply GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of the local simulator")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch (default: from config)")
    ap.add_argument("--demo", action="store_true",
                    help="local simulator only: the sim supply is found live at 2.5 A and adopted")
    args = ap.parse_args()

    if not args.connect:
        return run_local(theme=args.theme, demo=args.demo)

    # remote mode: drive a service through the client facade
    from alr3203.net.client import Alr3203Client
    # kind "gui": the service counts this window as a viewer, and the first
    # GUI to connect gets control (control.py / apps/control_bar.py)
    client = Alr3203Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                         kind="gui", name="alr3203 GUI")
    info = client.start()      # also pulls the service's config into client.cfg
    if args.theme:             # a local override wins over the service's stored theme
        client.cfg.ui.theme = args.theme
    print(f"connected to {args.connect}: {info.get('idn') or 'ALR3203'}")
    return run_app(client, client.cfg, remote=True)


if __name__ == "__main__":
    raise SystemExit(main())
