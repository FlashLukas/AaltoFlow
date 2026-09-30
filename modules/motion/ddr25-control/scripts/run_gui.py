"""Launch the ddr25 rotation-stage front panel.

Local (owns its own simulator brain in-process -- no service needed):
    python scripts/run_gui.py
    python scripts/run_gui.py --real            # drive the real stage directly

Remote (connect to a running service via ZeroMQ):
    python scripts/run_gui.py --connect 127.0.0.1

In remote mode the GUI is a thin client: identical controls, but every command
crosses the wire to the service that owns the hardware.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddr25.apps.gui import run_app  # noqa: E402
from ddr25.config import Config, load_config  # noqa: E402
from ddr25.net.protocol import (  # noqa: E402
    DEFAULT_CMD_PORT,
    DEFAULT_PUB_PORT,
    apply_config_dict,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="DDR25 rotation-stage GUI")
    ap.add_argument("--connect", metavar="HOST", help="connect to a remote service at HOST")
    ap.add_argument("--real", action="store_true", help="local mode: use the real stage")
    ap.add_argument("--config", help="INI config file to load (local mode)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the saved UI theme for this launch")
    args = ap.parse_args()

    if args.connect:
        from ddr25.net.client import Ddr25Client

        # kind "gui": the service counts this window as a viewer, and the first
        # GUI to connect gets control (control.py / apps/control_bar.py)
        client = Ddr25Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                             kind="gui", name="ddr25 GUI")
        client.start()
        # Mirror the service's config so the dial and Settings show its limits.
        cfg = Config()
        try:
            apply_config_dict(cfg, client.get_config())
        except Exception:
            pass
        if args.theme:
            cfg.ui.theme = args.theme
        try:
            sys.exit(run_app(client, cfg, remote=True))
        finally:
            client.close()

    cfg = load_config(args.config) if args.config else Config()
    if args.theme:
        cfg.ui.theme = args.theme
    from ddr25.sim_system import build_real_system, build_sim_system

    brain, _ = (build_real_system if args.real else build_sim_system)(cfg)
    brain.start()
    try:
        sys.exit(run_app(brain, cfg, remote=False))
    finally:
        brain.shutdown()


if __name__ == "__main__":
    main()
