"""Launch the spectrometer GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

The real CCS200, driven from this process (no service):
    uv run scripts/run_gui.py --real [--resource "USB0::0x1313::0x8089::M<serial>::RAW"]

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

from ccs200.config import Config
from ccs200.apps.gui import run_app
from ccs200.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="CCS200 spectrometer GUI (real or simulator)")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the spectrometer here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: drive the real CCS200 from this process")
    ap.add_argument("--resource", default=None, help="VISA resource for --real")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from ccs200.net.client import Ccs200Client
        client = Ccs200Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'ccs200')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from ccs200.spectrometer import Spectrometer
        from ccs200.backends import real_backend
        if args.resource:
            cfg.hardware.resource = args.resource
        return run_app(Spectrometer(real_backend(cfg), cfg), cfg)
    from ccs200.sim_system import build_sim_system
    spec, _ = build_sim_system(cfg)
    return run_app(spec, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
