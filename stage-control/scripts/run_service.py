"""Start the stage service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5559/5560
    python scripts/run_service.py --real          # real BSC203 via pylablib
    python scripts/run_service.py --config my.ini # load config first

The service binds to 0.0.0.0 so localhost and the lab Ethernet are the same
code -- a coordinator on another PC connects to this host's IP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running straight from a checkout without installing (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stage.config import Config, load_config  # noqa: E402
from stage.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from stage.net.service import StageService  # noqa: E402
from stage.sim_system import build_real_system, build_sim_system  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="3D coarse stage service")
    ap.add_argument("--real", action="store_true", help="use the real BSC203 (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = StageService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL BSC203" if args.real else "SIMULATOR"
    print(f"stage service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    service.serve_forever()


if __name__ == "__main__":
    main()
