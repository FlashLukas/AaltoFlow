"""Start the KIM001 focus service (simulator by default, --real for hardware).

    python scripts/run_service.py                 # simulator on 5637/5638
    python scripts/run_service.py --real          # real KIM001 via pylablib
    python scripts/run_service.py --config my.ini # load config first

Without --config the service loads ``kim001.ini`` from the kim001-control folder when
it exists (the step sizes saved by the save_calibration verb, e.g. after the
camera's "Calibrate Z steps"). Loading a file writes nothing to the controller:
its drive voltage / rate / acceleration are still ADOPTED at start.

The service binds to 0.0.0.0 so localhost and the lab Ethernet are the same
code -- a coordinator on another PC connects to this host's IP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running straight from a checkout without installing (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kim001.config import load_startup_config  # noqa: E402
from kim001.hwlock import HardwareBusy  # noqa: E402
from kim001.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402
from kim001.net.service import Kim001Service, PortInUse  # noqa: E402
from kim001.sim_system import build_real_system, build_sim_system  # noqa: E402

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="objective-focus inertia actuator service (KIM001/PIA13)")
    ap.add_argument("--real", action="store_true", help="use the real KIM001 (default: simulator)")
    ap.add_argument("--config", help="INI config file to load (default: kim001.ini in the "
                                     "kim001-control folder, if present)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--status-hz", type=float, default=8.0)
    # Which KIM001 (its Kinesis serial). Mission Control passes the one chosen
    # in "Instruments on this PC" (module.toml [hardware]); without the flag
    # hardware.serial from the config is used ("" = the one device found with
    # hardware.serial_prefix -- a guess, # VERIFY; an explicit serial is safer).
    ap.add_argument("--serial", default=None,
                    help="Kinesis serial of the KIM001 to open (default: from the config)")
    args = ap.parse_args()

    cfg, cfg_path, loaded = load_startup_config(args.config)
    if args.serial:
        cfg.hardware.serial = args.serial.strip()
    brain, _backend = (build_real_system if args.real else build_sim_system)(cfg)
    # save_calibration / save_config write back to the file we started from,
    # so what is saved is what the next start loads.
    brain.config_path = cfg_path

    service = Kim001Service(
        brain, host=args.host, cmd_port=args.cmd_port,
        pub_port=args.pub_port, status_hz=args.status_hz,
    )
    kind = "REAL KIM001" if args.real else "SIMULATOR"
    print(f"kim001 service [{kind}] on tcp://{args.host}:{args.cmd_port} (cmd) / {args.pub_port} (pub)")
    print(f"config: {cfg_path}" + ("" if loaded else " (not found -- defaults; "
                                                     "save_calibration creates it)"))
    print("Ctrl-C to stop.")
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"kim001 service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (a second kim001, or any module pointed at the same
        # serial -- kim on a KIM101, say) already drives this controller. One clear line, no
        # traceback, non-zero exit so the launcher shows the start failed.
        # Nothing is sent to the stage: the claim is taken in open() BEFORE
        # the USB link is opened, and brain.start() failed before it marked
        # itself connected, so its shutdown (stop every axis) never runs.
        print(f"kim001: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
