"""Launch the vector-magnet control GUI.

Local simulator (default -- a PRIVATE magnet in this window's own process):
    uv sync --extra gui
    uv run scripts/run_gui.py [--theme light]

Connect to a running service (same PC or across the lab network):
    uv run scripts/run_gui.py --connect localhost
    uv run scripts/run_gui.py --connect 192.168.1.42 --cmd-port 5577 --pub-port 5578

Without --connect you do NOT see the shared service; you see your own sim.
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from mag2dcal.apps.gui import main as run_local, run_app
from mag2dcal.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="mag2dcal vector magnet GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of the local simulator")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the saved UI theme for this launch")
    args = ap.parse_args()

    if not args.connect:
        return run_local(theme=args.theme)

    from mag2dcal.net.client import Mag2dcalClient
    client = Mag2dcalClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
    info = client.start()          # also pulls the service's config into client.cfg
    if not info:
        print(f"mag2dcal: no answer from {args.connect}:{args.cmd_port} -- is the service running?")
    if args.theme:
        client.cfg.ui.theme = args.theme
    return run_app(client, client.cfg, remote=True)


if __name__ == "__main__":
    raise SystemExit(main())
