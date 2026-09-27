"""Run the RF-amplifier control service.

    uv run scripts/run_service.py                       # simulated amplifier
    uv run scripts/run_service.py --real                # real amplifier, port from config
    uv run scripts/run_service.py --real --port COM7
    uv run scripts/run_service.py --cmd-port 5593 --pub-port 5594

The service owns the amplifier and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

At start the service only READS the amplifier (stage on/off, gain) and adopts
it; it switches the stage OFF when it stops (Ctrl-C, the launcher's Stop, the
`shutdown` verb). If another service already holds the COM port (hwlock), it
exits at once with one line on stderr and exit code 3. Drive it with:
    uv run scripts/dsamp_console.py --connect <host>

--real needs pyserial:  uv sync --extra gui --extra real
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dsamp.amplifier import Amplifier
from dsamp.config import Config
from dsamp.hwlock import HardwareBusy
from dsamp.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from dsamp.net.service import DsampService
from dsamp.sim_system import build_sim_system


def main() -> int:
    ap = argparse.ArgumentParser(description="DS Instruments RF amplifier control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real amplifier over its USB COM port (needs pyserial); "
                         "default is simulated")
    ap.add_argument("--port", default=None,
                    help="COM port of the real amplifier (default: from config, hardware.port)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from dsamp.backends.dsi_serial import DsiSerialAmp
        port = args.port or cfg.hardware.port
        backend = DsiSerialAmp(port, baud=cfg.hardware.baud,
                               timeout_s=cfg.hardware.timeout_s,
                               buttons_on_exit=cfg.hardware.buttons_on_exit)
        amp = Amplifier(backend, cfg)
        print(f"REAL backend -> {port}")
    else:
        amp, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = DsampService(amp, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service (another dsamp, or any module pointed at this COM
        # port) already holds the amplifier. We never opened it, so there is
        # nothing to switch off: say who holds it, in one line, and exit.
        print(f"dsamp: cannot start: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        # pyserial's SerialException is an OSError: wrong COM port, cable out,
        # port held by a non-AaltoFlow program. One readable line, no traceback.
        print(f"dsamp: cannot open the amplifier: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
