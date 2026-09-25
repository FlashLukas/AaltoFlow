"""Launch the piezo front panel.

Local (owns its own simulator brain in-process -- no service needed):
    python scripts/run_gui.py
    python scripts/run_gui.py --real            # drive real d-Drive directly

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

from piezo.apps.gui import run_app  # noqa: E402
from piezo.config import Config, load_config  # noqa: E402
from piezo.net.protocol import (  # noqa: E402
    DEFAULT_CMD_PORT,
    DEFAULT_PUB_PORT,
    apply_config_dict,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="2D piezo stage GUI")
    ap.add_argument("--connect", metavar="HOST", help="connect to a remote service at HOST")
    ap.add_argument("--real", action="store_true", help="local mode: use the real d-Drive")
    ap.add_argument("--config", help="INI config file to load (local mode)")
    ap.add_argument("--theme", choices=["dark", "light"],
                    help="override the startup GUI theme for this launch")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    args = ap.parse_args()

    if args.connect:
        # Remote: build a client and mirror the service's config into a local
        # Config just so the indicator/settings have sensible limits (this also
        # adopts the service's saved theme unless --theme overrides it).
        from piezo.net.client import PiezoClient

        client = PiezoClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        client.start()
        cfg = Config()
        try:
            apply_config_dict(cfg, client.get_config())
        except Exception:
            pass
        if args.theme:
            cfg.ui.theme = args.theme
        return sys.exit(run_app(client, cfg, remote=True))

    # Local: own the brain in-process.
    cfg = load_config(args.config) if args.config else Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from piezo.sim_system import build_real_system

        brain, _ = build_real_system(cfg)
    else:
        from piezo.sim_system import build_sim_system

        brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        sys.exit(run_app(brain, cfg, remote=False))
    finally:
        brain.shutdown()


if __name__ == "__main__":
    main()
