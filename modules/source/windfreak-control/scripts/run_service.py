"""Run the Windfreak SynthHD PRO v2 control service.

    uv run scripts/run_service.py                        # simulated synthesizer
    uv run scripts/run_service.py --real                 # real SynthHD (needs the extra "real")
    uv run scripts/run_service.py --real --port COM7
    uv run scripts/run_service.py --cmd-port 5583 --pub-port 5584

The service owns the synthesizer and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

At start the service only READS the synthesizer (RF on/off, frequency, power,
reference) and adopts it -- nothing is changed, a running output keeps
running. When it stops, both RF outputs are switched OFF. Drive it with:
    uv run scripts/windfreak_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from windfreak.config import Config
from windfreak.hwlock import HardwareBusy
from windfreak.synthesizer import Synthesizer
from windfreak.sim_system import build_sim_system
from windfreak.net.service import WindfreakService, PortInUse
from windfreak.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="Windfreak SynthHD PRO v2 control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real SynthHD over USB serial (needs pyserial); "
                         "default is simulated")
    ap.add_argument("--port", default=None,
                    help="COM port of the real SynthHD (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from windfreak.backends.synthhd import SerialSynthHD
        hw = cfg.hardware
        port = args.port or hw.port
        backend = SerialSynthHD(port, timeout_s=hw.timeout_s,
                                pll_off_when_rf_off=hw.pll_off_when_rf_off,
                                phase_command=hw.phase_command)
        synth = Synthesizer(backend, cfg)
        print(f"REAL backend -> {port}")
    else:
        synth, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = WindfreakService(synth, host=args.host, cmd_port=args.cmd_port,
                               pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"windfreak service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service already drives this COM port (hwlock). Say so in ONE
        # line -- the launcher shows it in its log -- and exit non-zero.
        # No shutdown / "RF off" here on purpose: the claim failed BEFORE the
        # port was opened (serve_forever's start() raised before its try), so
        # the SynthHD belongs to the other service and we must not touch its
        # outputs. ASCII only (gotcha #14).
        print(f"windfreak: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
