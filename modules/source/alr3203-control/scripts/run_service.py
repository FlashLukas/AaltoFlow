"""Run the ELC ALR3203 magnet-supply service.

    uv run scripts/run_service.py                       # simulated supply + coil + relay
    uv run scripts/run_service.py --real --com COM5     # the real supply
    uv run scripts/run_service.py --cmd-port 5635 --pub-port 5636

The service owns the supply and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)

With --real the polarity relay is driven through usb6001-control (config
[polarity] driver = usb6001, its host/port and the DAQ line): start that
service first. [polarity] driver = none runs the supply unipolar.

At start the service READS the supply (setpoints, output on/off, what it
regulates) and the relay, and adopts them -- nothing is written. Every way the
service stops (Ctrl-C, the `shutdown` command, the launcher's Stop) ramps the
output to zero and switches it off first. Drive it with:
    uv run scripts/alr3203_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from alr3203.config import Config
from alr3203.supply import BipolarSupply
from alr3203.sim_system import build_real_relay, build_sim_system
from alr3203.net.service import Alr3203Service, PortInUse
from alr3203.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from alr3203.hwlock import HardwareBusy

# the config this PC uses: next to module.toml (gitignored, lab data)
_DEFAULT_INI = os.path.join(os.path.dirname(__file__), "..", "alr3203.ini")

# Exit code when the supply's COM port is already held by another service.
# Distinct from 2 (bad arguments / cannot start) so a launcher can tell them apart.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="ELC ALR3203 magnet supply control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real ALR3203 over its USB serial port (needs the "
                         "`real` extra); default is simulated")
    ap.add_argument("--com", default=None,
                    help="COM port of the supply (default: from config [hardware] com)")
    ap.add_argument("--config", default=None,
                    help="path to a .ini config to load (default: alr3203.ini next to "
                         "module.toml, if it exists)")
    args = ap.parse_args()

    path = args.config or _DEFAULT_INI
    cfg = Config.load(path) if os.path.isfile(path) else Config()

    if args.real:
        from alr3203.backends.elc_serial import SerialALR3203
        port = args.com or cfg.hardware.com
        backend = SerialALR3203(port, baud=cfg.hardware.baud,
                                timeout_s=cfg.hardware.timeout_s)
        try:
            relay = build_real_relay(cfg)
        except ValueError as exc:
            print(f"alr3203 service: cannot start: {exc}", file=sys.stderr)
            return 2
        supply = BipolarSupply(backend, cfg, relay=relay)
        print(f"REAL backend -> {port or '(no COM port set)'}; relay: {relay.describe()}")
    else:
        supply, _ = build_sim_system(cfg)
        print("SIMULATED backend (an ALR3203 driving a coil through a relay; "
              "no hardware needed)")

    # Said on the console too (the start event goes only to clients): a cap
    # set on one sign only is easy to miss (Blue Box, 2026-11-10).
    note = supply.envelope_note()
    if note:
        print(f"WARNING: {note}")

    service = Alr3203Service(supply, host=args.host, cmd_port=args.cmd_port,
                             pub_port=args.pub_port)
    # The real backend claims its COM port before it sends anything (hwlock).
    # If another service holds it, open() raises HardwareBusy: ONE plain line
    # on stderr, no traceback, and nothing written to a supply we never opened.
    # (It comes out of service.start(), before serve_forever's try/finally, so
    # the "ramp to zero + output off" of stop() does NOT run -- deliberately.)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (gotcha #7). Nothing was opened:
        # the sockets are bound BEFORE the instrument (gotcha #39).
        print(f"alr3203 service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as e:
        print(f"alr3203: cannot start: {e}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except RuntimeError as e:
        # no COM port, pyserial missing, wrong instrument on the port, state
        # unreadable: one line, not a stack
        print(f"alr3203: cannot start: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
