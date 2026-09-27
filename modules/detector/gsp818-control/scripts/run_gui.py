"""Launch the spectrum analyser GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

The real GSP-818, driven from this process (no service):
    uv sync --extra gui --extra real
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

from gsp818.config import Config
from gsp818.apps.gui import run_app
from gsp818.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Spectrum analyser GUI (GSP-818 or simulator)")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the analyser here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: drive the real GSP-818 from this process")
    ap.add_argument("--visa", default=None, help="VISA address for --real")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from gsp818.net.client import Gsp818Client
        client = Gsp818Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'gsp818')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from gsp818.analyzer import SpectrumAnalyzer
        from gsp818.backends import real_backend
        if args.visa:
            cfg.hardware.resource = args.visa
        return run_app(SpectrumAnalyzer(real_backend(cfg), cfg), cfg)
    from gsp818.sim_system import build_sim_system
    sa, _ = build_sim_system(cfg)
    return run_app(sa, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
