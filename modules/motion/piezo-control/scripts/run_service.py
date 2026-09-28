"""Start the piezo service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5561/5562
    python scripts/run_service.py --real          # real d-Drive over serial
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

from piezo.config import Config, load_config  # noqa: E402
from piezo.hwlock import HardwareBusy  # noqa: E402
from piezo.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from piezo.net.service import PiezoService, PortInUse  # noqa: E402
from piezo.sim_system import build_real_system, build_sim_system  # noqa: E402

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="2D piezo stage service")
    ap.add_argument("--real", action="store_true", help="use the real d-Drive (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = PiezoService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL d-Drive" if args.real else "SIMULATOR"
    print(f"piezo service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"piezo service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (a second piezo, or any module pointed at the same
        # COM port) already holds the d-Drive.  We never opened the port, so
        # there is nothing to close and no command was sent: say who holds
        # it, in one line (the launcher shows stderr in its log), and exit.
        # serve_forever's cleanup calls brain.shutdown(), which returns at
        # once because the brain never connected -- no "safe state" is sent
        # to an instrument we do not own.
        print(f"piezo: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
