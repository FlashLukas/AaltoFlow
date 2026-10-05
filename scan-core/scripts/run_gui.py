"""The GUI of the "Scan server" card: the measurement suite, watching that server.

    uv run --extra gui scripts/run_gui.py                       # this PC's scan server
    uv run --extra gui scripts/run_gui.py --connect lab-pc      # another PC's
    uv run --extra gui scripts/run_gui.py --connect lab-pc --cmd-port 5631 --pub-port 5632

Mission Control passes --connect/--cmd-port/--pub-port (the card's contract,
INSTRUMENT_MODULE_GUIDE section 11): a card added with "Add remote..." on the
office PC opens the suite here with its Measurement tab watching the lab PC's
scan -- progress, the live map, the log, the pause banners, Abort.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan_core.scan_server import DEFAULT_CMD_PORT  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Measurement suite watching a scan server")
    ap.add_argument("--connect", metavar="HOST", default="localhost",
                    help="the PC the scan server runs on (default: this one)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=None)
    ap.add_argument("--theme", choices=["dark", "light"], default=None)
    args = ap.parse_args()
    from apps.suite import main as suite_main
    target = f"{args.connect}:{args.cmd_port}" + (f":{args.pub_port}" if args.pub_port else "")
    argv = ["--scan-server", target]
    if args.theme:
        argv += ["--theme", args.theme]
    return suite_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
