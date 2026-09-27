"""Start the ddr25 rotation-stage service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5605/5606
    python scripts/run_service.py --real          # real K-Cube + DDR25 via pylablib
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

from ddr25.config import Config, load_config  # noqa: E402
from ddr25.hwlock import HardwareBusy  # noqa: E402
from ddr25.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from ddr25.net.service import Ddr25Service  # noqa: E402
from ddr25.sim_system import build_real_system, build_sim_system  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="DDR25 rotation-stage service")
    ap.add_argument("--real", action="store_true",
                    help="use the real K-Cube + DDR25 (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=10.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = Ddr25Service(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL K-Cube + DDR25" if args.real else "SIMULATOR"
    print(f"ddr25 service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service already drives this K-Cube (same Kinesis serial,
        # hwlock.py). Say so in ONE line -- the launcher shows it in its
        # log -- and exit non-zero, without a traceback. Nothing to make
        # safe: the claim failed BEFORE the controller was opened, so the
        # brain never connected and its shutdown sends no stop command to a
        # stage that belongs to the other service. (ASCII: gotcha #14.)
        print(f"ddr25: cannot start: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
