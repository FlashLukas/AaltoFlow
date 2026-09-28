"""Run the Signal Recovery 7230 lock-in control service.

    uv run scripts/run_service.py                                # simulated lock-in
    uv run scripts/run_service.py --real --address 192.168.0.50  # the real 7230 over Ethernet
    uv run scripts/run_service.py --config sr7230.ini

Without --config, `sr7230.ini` in the project folder is loaded when it exists.
That is how the launcher's "real" tick can work at all: Mission Control passes
only --real, and the real 7230 cannot be reached without its IP address
(hardware.host). Save the address there once (GUI Settings > Save, or by hand).

The service owns the lock-in and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5621   (REP)
  * status   on tcp://0.0.0.0:5622   (PUB, 10 Hz)

Drive it with:
    uv run scripts/sr7230_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from sr7230.config import Config
from sr7230.lockin import LockIn
from sr7230.sim_system import build_sim_system
from sr7230.net.service import Sr7230Service
from sr7230.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from sr7230.hwlock import HardwareBusy

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4

#: this PC's settings (the instrument's IP address above all); gitignored,
#: and kept by the installer across upgrades
_DEFAULT_INI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sr7230.ini"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal Recovery 7230 lock-in control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real 7230 over Ethernet; default is simulated")
    ap.add_argument("--address", default=None,
                    help="IP address of the real 7230 (default: hardware.host from the config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    config_path = args.config
    if config_path is None and os.path.isfile(_DEFAULT_INI):
        config_path = _DEFAULT_INI
    cfg = Config.load(config_path) if config_path else Config()
    if config_path:
        print(f"config: {config_path}")

    if args.real:
        from sr7230.backends.tcp7230 import Tcp7230
        hw = cfg.hardware
        if args.address:
            hw.host = args.address
        try:
            backend = Tcp7230(hw.host, port=hw.port, timeout_s=hw.timeout_s,
                              interface=hw.interface)
        except ValueError as exc:
            print(f"cannot start: {exc}")
            return 2
        lockin = LockIn(backend, cfg)
        print(f"REAL backend -> 7230 at {hw.host}:{hw.port}")
    else:
        lockin, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    try:
        Sr7230Service(lockin, host=args.host, cmd_port=args.cmd_port,
                      pub_port=args.pub_port).serve_forever()
    except HardwareBusy as exc:
        # Another service already drives this 7230 (same IP address). The
        # backend refused BEFORE connecting, so there is nothing to close or
        # make safe here -- in particular no "OSC OUT to 0 V": that box is
        # not ours. The brain's start() raised before the service threads
        # existed, so no stop()/shutdown() ran either. One line, no traceback,
        # non-zero exit so the launcher shows the start as failed.
        print(f"cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
