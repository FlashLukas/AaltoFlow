"""Launch the gaussmeter GUI.

Local simulator (default):
    uv sync --extra gui
    uv run scripts/run_gui.py

The real meter, in-process (no service):
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

from ls455.config import Config
from ls455.apps.gui import run_app
from ls455.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Lake Shore 455 gaussmeter GUI")
    ap.add_argument("--connect", metavar="HOST", default=None,
                    help="connect to a service at HOST instead of running the meter here")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--sim-probe", choices=["HSE", "HST", "UHS"], default="HSE",
                    help="simulator only: which probe family the simulated meter has "
                         "(the geometry is hardware.probe_geometry, axial by default)")
    ap.add_argument("--real", action="store_true",
                    help="without --connect: open the real meter in this process")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up theme for this launch")
    args = ap.parse_args()

    if args.connect:
        from ls455.net.client import Ls455Client
        client = Ls455Client(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
        info = client.start()      # also pulls the service's config into client.cfg
        if args.theme:
            client.cfg.ui.theme = args.theme
        print(f"connected to {args.connect}: {info.get('idn', 'gaussmeter')}")
        return run_app(client, client.cfg, remote=True)

    cfg = Config()
    if args.theme:
        cfg.ui.theme = args.theme
    if args.real:
        from ls455.backends.ls455 import LakeShore455
        from ls455.gaussmeter import Gaussmeter
        hw = cfg.hardware
        meter = Gaussmeter(LakeShore455(hw.resource, baud_rate=hw.baud_rate,
                                        timeout_ms=hw.timeout_ms,
                                        command_gap_s=hw.command_gap_s,
                                        zero_time_s=hw.zero_time_s), cfg)
    else:
        from ls455.sim_system import build_sim_system
        meter, _ = build_sim_system(cfg, probe=args.sim_probe)
    return run_app(meter, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
