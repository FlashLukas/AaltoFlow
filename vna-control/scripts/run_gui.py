"""Launch the VNA GUI.

Local simulator (default; follows mag2d if it is running, else the manual field):
    uv sync --extra gui
    uv run scripts/run_gui.py
    uv run scripts/run_gui.py --field manual

The real PNA-X, driven from this process (no service):
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

from vna.config import Config
from vna.field import FIELD_SOURCES
from vna.apps.gui import run_app
from vna.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="VNA GUI (Keysight PNA-X or simulator)")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the analyser here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: drive the real PNA-X from this process")
    ap.add_argument("--visa", default=None, help="VISA resource/alias for --real")
    ap.add_argument("--field", choices=list(FIELD_SOURCES), default=None,
                    help="without --connect: where the field comes from")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from vna.net.client import VnaClient
        client = VnaClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'vna')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.field:
        cfg.field.source = args.field
    if args.real:
        from vna.analyzer import Analyzer
        from vna.backends.pna import PnaVna
        if args.visa:
            cfg.hardware.visa_resource = args.visa
        return run_app(Analyzer(PnaVna(cfg), cfg), cfg)
    from vna.sim_system import build_sim_system
    vna, _ = build_sim_system(cfg)
    return run_app(vna, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
