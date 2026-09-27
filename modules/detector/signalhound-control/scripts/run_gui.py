"""Launch the spectrum-analyser GUI.

Local simulator (default: an SA44B with a TG44A):
    uv sync --extra gui
    uv run scripts/run_gui.py
    uv run scripts/run_gui.py --model SA124B

A real Signal Hound, driven from this process (no service):
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

from signalhound.config import Config
from signalhound.apps.gui import run_app
from signalhound.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Spectrum analyser GUI (real Signal Hound or simulator)")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the analyser here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: drive the real analyser from this process")
    ap.add_argument("--model", choices=["auto", "SA44B", "SA124B"], default=None,
                    help="without --connect: expected (real) or simulated model")
    ap.add_argument("--serial", type=int, default=None, help="serial number for --real")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from signalhound.net.client import SignalhoundClient
        client = SignalhoundClient(host=args.connect, cmd_port=args.cmd_port,
                                   pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'signalhound')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.model:
        cfg.hardware.model = args.model
    if args.real:
        from signalhound.spectrum import SpectrumAnalyzer
        from signalhound.backends import real_backend
        if args.serial is not None:
            cfg.hardware.serial = args.serial
        return run_app(SpectrumAnalyzer(real_backend(cfg), cfg), cfg)
    from signalhound.sim_system import build_sim_system
    signalhound, _ = build_sim_system(cfg)
    return run_app(signalhound, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
