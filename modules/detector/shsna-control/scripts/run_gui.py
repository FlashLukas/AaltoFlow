"""Launch the scalar network analyser GUI.

Local simulator (default; standalone, no analyser needed):
    uv sync --extra gui
    uv run scripts/run_gui.py

TG sweeps through the signalhound service, from this process (no shsna service):
    uv run scripts/run_gui.py --real [--owner-host HOST]

Connect to a running shsna service (same PC or across the lab network):
    uv run scripts/run_gui.py --connect localhost
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from shsna.config import Config
from shsna.apps.gui import run_app
from shsna.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="scalar network analyser GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the brain here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: measure through the signalhound service")
    ap.add_argument("--owner-host", default=None, help="with --real: host of the signalhound service")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from shsna.net.client import ShsnaClient
        # kind "gui": the service counts this window as a viewer, and the first
        # GUI to connect gets control (control.py / apps/control_bar.py)
        client = ShsnaClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                             kind="gui", name="shsna GUI")
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'shsna')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from shsna.analyzer import Analyzer
        from shsna.backends import real_backend
        if args.owner_host:
            cfg.hardware.owner_host = args.owner_host
        return run_app(Analyzer(real_backend(cfg), cfg), cfg)
    from shsna.apps.gui import main as sim_main
    return sim_main(theme=args.theme)


if __name__ == "__main__":
    raise SystemExit(main())
