"""Start the SmarAct positioner service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5597/5598
    python scripts/run_service.py --real          # real SCU via the SmarAct DLL
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

from smaract.config import Config, load_config  # noqa: E402
from smaract.hwlock import HardwareBusy  # noqa: E402
from smaract.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from smaract.net.service import SmaractService  # noqa: E402
from smaract.sim_system import build_real_system, build_sim_system  # noqa: E402

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def _ascii(text: str) -> str:
    """Printed text stays ASCII (gotcha #14): an error text from Windows can be
    localised, and a non-ASCII character on a pipe kills the print itself."""
    return text.encode("ascii", "replace").decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser(description="SmarAct linear positioner service")
    ap.add_argument("--real", action="store_true", help="use the real SCU controller (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = SmaractService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL SCU" if args.real else "SIMULATOR"
    print(f"smaract service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print("Ctrl-C to stop.")
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (a second smaract, or anything else pointed at this
        # SCU) already holds it. We never got the controller, so there is
        # nothing to stop or close: say who holds it, in ONE line, and exit.
        # (serve_forever's cleanup is not reached: start() raised first.)
        print(_ascii(f"smaract: cannot start: {exc}"), file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except RuntimeError as exc:
        # ScuError (no DLL, no SCU, no sensor, wrong sensor type): one
        # readable line in the launcher log instead of a traceback wall.
        print(_ascii(f"smaract: cannot start: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
