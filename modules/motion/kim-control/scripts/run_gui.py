"""Launch the KIM101 stage front panel.

Local (owns its own simulator brain in-process -- no service needed):
    python scripts/run_gui.py
    python scripts/run_gui.py --real            # drive the real KIM101 directly

Remote (connect to a running service via ZeroMQ):
    python scripts/run_gui.py --connect 127.0.0.1
    python scripts/run_gui.py --connect 192.168.0.5

In remote mode the GUI is a thin client: identical controls, but every command
crosses the wire to the service that owns the hardware.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kim.apps.gui import run_app  # noqa: E402
from kim.config import Config, load_config  # noqa: E402
from kim.net.protocol import (  # noqa: E402
    DEFAULT_CMD_PORT,
    DEFAULT_PUB_PORT,
    apply_config_dict,
)


def main(theme: str | None = None) -> None:
    ap = argparse.ArgumentParser(description="3D piezo-inertia stage GUI (KIM101/PIA25)")
    ap.add_argument("--connect", metavar="HOST", help="connect to a remote service at HOST")
    ap.add_argument("--real", action="store_true", help="local mode: use the real KIM101")
    ap.add_argument("--config", help="INI config file to load (local mode)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"],
                    help="override the startup theme for this launch")
    args = ap.parse_args()
    # An explicit function arg wins, else the CLI flag (either may be None).
    theme = theme or args.theme

    if args.connect:
        # Remote: build a client and mirror the service's config into a local
        # Config just so the indicator/settings have sensible limits.
        from kim.net.client import KimClient

        client = KimClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        client.start()
        cfg = Config()
        try:
            apply_config_dict(cfg, client.get_config())
        except Exception:
            pass
        if theme:
            cfg.ui.theme = theme
        return sys.exit(run_app(client, cfg, remote=True))

    # Local: own the brain in-process.
    cfg = load_config(args.config) if args.config else Config()
    if theme:
        cfg.ui.theme = theme
    if args.real:
        from kim.sim_system import build_real_system

        brain, _ = build_real_system(cfg)
    else:
        from kim.sim_system import build_sim_system

        brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        sys.exit(run_app(brain, cfg, remote=False))
    finally:
        brain.shutdown()


if __name__ == "__main__":
    main()
