"""Launch the camera GUI.

    python scripts/run_gui.py                     # local: sim brain in-process
    python scripts/run_gui.py --connect 192.168.0.5   # remote: drive a service

Local mode builds the simulated system and drives the brain directly.  Remote
mode connects a CameraClient to a running service (start it with run_service.py)
-- the GUI is identical either way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from camera.config import Config, load_config  # noqa: E402
from camera.net import protocol as P           # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="camera GUI")
    ap.add_argument("--connect", default="", metavar="HOST",
                    help="connect to a remote service instead of a local sim")
    ap.add_argument("--config", default="")
    # --cmd-port/--pub-port is the suite-wide spelling; --cmd/--pub still work.
    ap.add_argument("--cmd-port", "--cmd", dest="cmd", type=int, default=P.DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", "--pub", dest="pub", type=int, default=P.DEFAULT_PUB_PORT)
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="override the start-up GUI theme for this launch")
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()

    from camera.apps.gui import run_app

    if args.connect:
        from camera.net.client import CameraClient
        ctrl = CameraClient(args.connect, args.cmd, args.pub)
        ctrl.start()
        # populate the settings forms with the server's current config
        try:
            data = ctrl.get_config()
            for gname, values in (data or {}).items():
                obj = getattr(cfg, gname, None)
                if obj is not None:
                    for k, v in values.items():
                        if hasattr(obj, k):
                            setattr(obj, k, v)
        except Exception:
            pass
        if args.theme:                     # --theme overrides for this launch
            cfg.ui.theme = args.theme
        code = run_app(ctrl, cfg, remote=True)
        ctrl.close()
    else:
        from camera.sim_system import build_sim_system
        brain, *_ = build_sim_system(cfg)
        brain.start()
        if args.theme:                     # --theme overrides for this launch
            cfg.ui.theme = args.theme
        code = run_app(brain, cfg, remote=False)
        brain.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()
