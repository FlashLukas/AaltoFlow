"""Start the Agilis stage service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5595/5596
    python scripts/run_service.py --real          # real AG-UC2 (needs hardware.port in --config)
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

from agilis.config import Config, load_config  # noqa: E402
from agilis.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from agilis.net.service import AgilisService  # noqa: E402
from agilis.sim_system import build_real_system, build_sim_system  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Newport Agilis stage service (AG-UC2, 2 axes)")
    ap.add_argument("--real", action="store_true", help="use the real AG-UC2 (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--com", help="the AG-UC2 COM port, e.g. COM5 (overrides the config)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    if args.com:
        cfg.hardware.port = args.com
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = AgilisService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = f"REAL AG-UC2 {cfg.hardware.port}" if args.real else "SIMULATOR"
    print(f"agilis service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    service.serve_forever()


if __name__ == "__main__":
    main()
