"""Start the Z-piezo service (simulator by default, --real for the KCube).

    python scripts/run_service.py            # simulator
    python scripts/run_service.py --real     # Thorlabs KCube
    python scripts/run_service.py --cmd 5565 --pub 5566
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zpiezo.config import Config, load_config       # noqa: E402
from zpiezo.net import protocol as P                # noqa: E402
from zpiezo.net.service import ZPiezoService        # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Z-piezo service")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--config", default="")
    ap.add_argument("--host", default="0.0.0.0")
    # --cmd-port/--pub-port is the suite-wide spelling; --cmd/--pub still work.
    ap.add_argument("--cmd-port", "--cmd", dest="cmd", type=int, default=P.DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", "--pub", dest="pub", type=int, default=P.DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    if args.real:
        from zpiezo.sim_system import build_real_system
        brain, _ = build_real_system(cfg)
        print("z-piezo service: REAL KCube")
    else:
        from zpiezo.sim_system import build_sim_system
        brain, _ = build_sim_system(cfg)
        print("z-piezo service: SIMULATOR")

    svc = ZPiezoService(brain, host=args.host, cmd_port=args.cmd,
                        pub_port=args.pub, status_hz=args.status_hz)
    print(f"  commands tcp://{args.host}:{args.cmd}   status tcp://{args.host}:{args.pub}")
    print("  Ctrl-C to stop.")
    svc.serve_forever()


if __name__ == "__main__":
    main()
