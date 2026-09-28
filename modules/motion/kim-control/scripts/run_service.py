"""Start the KIM101 stage service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5567/5568
    python scripts/run_service.py --real          # real KIM101 via pylablib
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

from kim.config import Config, load_config  # noqa: E402
from kim.hwlock import HardwareBusy  # noqa: E402
from kim.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from kim.net.service import KimService, PortInUse  # noqa: E402
from kim.sim_system import build_real_system, build_sim_system  # noqa: E402

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="3D piezo-inertia stage service (KIM101/PIA25)")
    ap.add_argument("--real", action="store_true", help="use the real KIM101 (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = KimService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL KIM101" if args.real else "SIMULATOR"
    print(f"kim service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"kim service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (a second kim, or any module pointed at the same
        # KIM101 serial) already drives this controller. One clear line, no
        # traceback, non-zero exit so the launcher shows the start failed.
        # Nothing is sent to the stage: the claim is taken in open() BEFORE
        # the USB link is opened, and brain.start() failed before it marked
        # itself connected, so its shutdown (stop every axis) never runs.
        print(f"kim: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
