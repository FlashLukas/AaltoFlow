"""Run the function generator (Tektronix AFG1062) control service.

    uv run scripts/run_service.py                        # simulated AFG1062
    uv run scripts/run_service.py --real                 # real AFG (needs the extra "real")
    uv run scripts/run_service.py --real --visa "USB0::0x0699::0x0353::C012345::INSTR"
    uv run scripts/run_service.py --cmd-port 5631 --pub-port 5632

The service owns the generator and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

At start the service only READS the generator (both channels: waveform,
frequency, amplitude, offset, phase, load, output on/off) and adopts it --
nothing is changed, a running output keeps running. When it stops, both
outputs are switched OFF. Drive it with:
    uv run scripts/afg_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from afg.config import Config
from afg.hwlock import HardwareBusy
from afg.generator import Generator
from afg.sim_system import build_sim_system
from afg.net.service import AfgService, PortInUse
from afg.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="Function generator (AFG1062) control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real AFG1062 over USB (needs pyvisa + a VISA "
                         "library); default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the real AFG (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from afg.backends.tek_afg import TekAFG
        hw = cfg.hardware
        resource = args.visa or hw.visa
        backend = TekAFG(resource, timeout_ms=hw.timeout_ms, phase_unit=hw.phase_unit)
        gen = Generator(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        gen, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = AfgService(gen, host=args.host, cmd_port=args.cmd_port,
                         pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"afg service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service already drives this VISA address (hwlock). Say so in
        # ONE line and exit non-zero. No shutdown / "outputs off" here on
        # purpose: the claim failed BEFORE the instrument was opened, so the
        # AFG belongs to the other service and we must not touch its outputs.
        # ASCII only (gotcha #14).
        print(f"afg: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
