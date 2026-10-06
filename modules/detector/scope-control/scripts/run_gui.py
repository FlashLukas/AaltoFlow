"""Launch the oscilloscope GUI.

    uv run scripts/run_gui.py                     # a private simulated bench
    uv run scripts/run_gui.py --connect localhost # the running service (the usual way)
    uv run scripts/run_gui.py --real --visa "USB0::0xF4EC::..."   # the scope, no service
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from scope.config import Config
from scope.apps.gui import run_app
from scope.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Oscilloscope GUI (real or simulator)")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the scope here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="without --connect: drive the real scope from this process")
    ap.add_argument("--visa", default=None, help="VISA resource for --real")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        # --real is ignored here: the SERVICE decides what it drives, and the
        # GUI shows whichever it is.
        from scope.net.client import ScopeClient
        # kind "gui": the service counts this window as a viewer, and the first
        # GUI to connect gets control (control.py / apps/control_bar.py)
        client = ScopeClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                              kind="gui", name="scope GUI")
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'scope')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from scope.scope import Scope
        from scope.backends.siglent import SiglentSDS
        if args.visa:
            cfg.hardware.visa = args.visa
        return run_app(Scope(SiglentSDS(cfg.hardware.visa, cfg.hardware.timeout_ms), cfg), cfg)
    from scope.sim_system import build_sim_system
    spec, _ = build_sim_system(cfg)
    return run_app(spec, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
