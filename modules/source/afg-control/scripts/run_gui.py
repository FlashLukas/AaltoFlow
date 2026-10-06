"""Launch the function generator (Tektronix AFG1062) front panel.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

Connect to a running service (same PC or across the lab network):
    uv run scripts/run_gui.py --connect 192.168.1.42
    uv run scripts/run_gui.py --connect localhost --cmd-port 5631 --pub-port 5632
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from afg.apps.gui import main as run_local, run_app
from afg.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Function generator (AFG1062) GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of the local simulator")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch (default: from config)")
    args = ap.parse_args()

    if not args.connect:
        return run_local(theme=args.theme)

    # remote mode: drive a service through the client facade
    from afg.net.client import AfgClient
    # kind "gui": the service counts this window as a viewer, and the first
    # GUI to connect gets control (control.py / apps/control_bar.py)
    client = AfgClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                             kind="gui", name="afg GUI")
    info = client.start()      # also pulls the service's config into client.cfg
    if args.theme:             # a local override wins over the service's stored theme
        client.cfg.ui.theme = args.theme
    print(f"connected to {args.connect}: {info.get("idn", "AFG")}")
    return run_app(client, client.cfg, remote=True)


if __name__ == "__main__":
    raise SystemExit(main())
