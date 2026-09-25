"""Launch the lock-in GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py [--theme light]

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

from hf2.apps.gui import main as run_local, run_app
from hf2.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="HF2LI lock-in GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of the local simulator")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if not args.connect:
        return run_local(theme=args.theme)

    from hf2.net.client import Hf2Client
    client = Hf2Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
    info = client.start()          # also pulls the service's config into client.cfg
    if args.theme:
        client.cfg.ui.theme = args.theme
    print(f"connected to {args.connect}: {info.get('idn', 'HF2LI')}")
    return run_app(client, client.cfg, remote=True)


if __name__ == "__main__":
    raise SystemExit(main())
