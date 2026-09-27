"""Start the elliptec service (simulator by default, --real for hardware).

    python scripts/run_service.py                          # simulator on 5607/5608
    python scripts/run_service.py --real --port COM5       # real ELL14 mount(s) via pyserial
    python scripts/run_service.py --addresses 0,1          # two mounts on one bus
    python scripts/run_service.py --config my.ini          # load config first

The service binds to 0.0.0.0 so localhost and the lab Ethernet are the same
code -- a coordinator on another PC connects to this host's IP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running straight from a checkout without installing (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elliptec.config import Config, load_config  # noqa: E402
from elliptec.hwlock import HardwareBusy  # noqa: E402
from elliptec.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from elliptec.net.service import ElliptecService  # noqa: E402
from elliptec.sim_system import build_real_system, build_sim_system  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Elliptec rotation mount service")
    ap.add_argument("--real", action="store_true", help="use the real mounts over pyserial (default: simulator)")
    ap.add_argument("--config", help="INI config file to load")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    ap.add_argument("--port", help="serial port of the interface board, e.g. COM5 (overrides the config)")
    ap.add_argument("--addresses", help="bus addresses, e.g. 0 or 0,1 (overrides the config)")
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else Config()
    if args.port:
        cfg.hardware.port = args.port
    if args.addresses:
        cfg.axes.addresses = args.addresses
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)

    service = ElliptecService(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = f"REAL, {cfg.hardware.port}" if args.real else "SIMULATOR"
    print(f"elliptec service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print(f"mounts on bus addresses: {cfg.axes.addresses}.  Ctrl-C to stop.")
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (this module or any other pointed at the same
        # ELL14K board) already holds the COM port. One plain line, no
        # traceback: the message names the port and the holder. Nothing to
        # stop or close -- the claim failed BEFORE the port was opened, so no
        # byte went to the mounts; the brain never became "connected", so its
        # shutdown sends no stop commands to a bus that is not ours.
        print(f"elliptec service: could not start: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
