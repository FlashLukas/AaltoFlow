"""Start the camera service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulated closed-loop scene
    python scripts/run_service.py --real          # GenICam cam + KCube Z + piezo XY
    python scripts/run_service.py --cmd-port 5563 --pub-port 5564

The service binds to 0.0.0.0 so localhost and the lab Ethernet are the same code.

When the launcher starts it, the environment variable AALTOFLOW_ENDPOINTS says
where every other module listens (JSON: {"kim": ["localhost", 5567, 5568], ...}).
The camera drives kim / piezo / zpiezo over the network, so a port changed in the
launcher must reach it -- otherwise the camera would keep calling the old port
from camera.ini and the stage would simply never move.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from camera.config import Config, load_config  # noqa: E402
from camera.net import protocol as P           # noqa: E402
from camera.net.service import CameraService   # noqa: E402


#: camera.ini field prefix for each module the camera talks to
_PEERS = {"kim": "kim", "piezo": "piezo", "zpiezo": "z"}


def apply_launcher_endpoints(cfg) -> None:
    """Point the camera at the ports the launcher is actually using.

    Only overrides what the launcher passed; started by hand (no variable), the
    camera uses camera.ini exactly as before.
    """
    # AALTOFLOW_ENDPOINTS since the 2026-09-24 rename; the old name still works
    raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
    if not raw:
        return
    try:
        endpoints = json.loads(raw)
    except ValueError:
        print("camera service: AALTOFLOW_ENDPOINTS is not valid JSON, ignored")
        return
    hw = cfg.hardware
    for key, field in _PEERS.items():
        ep = endpoints.get(key)
        if not ep or len(ep) != 3:
            continue
        host, cmd, pub = ep
        setattr(hw, f"{field}_host", "127.0.0.1" if host == "localhost" else str(host))
        setattr(hw, f"{field}_cmd_port", int(cmd))
        setattr(hw, f"{field}_pub_port", int(pub))
        print(f"camera service: {key} at {host}:{cmd}/{pub} (from the launcher)")


def main() -> None:
    ap = argparse.ArgumentParser(description="camera vision service")
    ap.add_argument("--real", action="store_true", help="use real hardware backends")
    ap.add_argument("--config", default="", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    # --cmd-port/--pub-port is the suite-wide spelling; --cmd/--pub still work.
    ap.add_argument("--cmd-port", "--cmd", dest="cmd", type=int, default=P.DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", "--pub", dest="pub", type=int, default=P.DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    # No --config: use camera.ini in the project folder if one was saved (the GUI's
    # Spot tab "Save" writes it), so thresholds and the spot position persist.
    default_ini = Path(__file__).resolve().parents[1] / "camera.ini"
    if not args.config and default_ini.is_file():
        args.config = str(default_ini)
    cfg = load_config(args.config) if args.config else Config()
    if args.config:
        print(f"camera service: config <- {args.config}")
    apply_launcher_endpoints(cfg)

    if args.real:
        from camera.sim_system import build_real_system
        brain, *_ = build_real_system(cfg)
        print("camera service: REAL hardware")
    else:
        from camera.sim_system import build_sim_system
        brain, *_ = build_sim_system(cfg)
        print("camera service: SIMULATOR (synthetic closed-loop scene)")

    svc = CameraService(brain, host=args.host, cmd_port=args.cmd,
                        pub_port=args.pub, status_hz=args.status_hz)
    print(f"  commands tcp://{args.host}:{args.cmd}   status tcp://{args.host}:{args.pub}")
    print("  Ctrl-C to stop.")
    svc.serve_forever()


if __name__ == "__main__":
    main()
