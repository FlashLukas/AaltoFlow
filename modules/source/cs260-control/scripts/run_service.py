"""Run the Cornerstone 260 monochromator service.

    uv run scripts/run_service.py                       # simulated monochromator
    uv run scripts/run_service.py --real                # real CS260 over GPIB
    uv run scripts/run_service.py --real --visa GPIB0::4::INSTR
    uv run scripts/run_service.py --cmd-port 5601 --pub-port 5602

The service owns the instrument and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

Drive it with:
    uv run scripts/mono_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from cs260.config import Config
from cs260.hwlock import HardwareBusy
from cs260.monochromator import Monochromator
from cs260.sim_system import build_sim_system
from cs260.net.service import Cs260Service
from cs260.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

#: Loaded automatically when present, so a lab PC keeps its grating / accessory
#: description without a command-line flag.
DEFAULT_INI = os.path.join(os.path.dirname(__file__), "..", "cs260.ini")


def main() -> int:
    ap = argparse.ArgumentParser(description="Cornerstone 260 monochromator control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real CS260 over GPIB (needs the 'real' extra: pyvisa); "
                         "default is simulated")
    ap.add_argument("--visa", default=None,
                    help="VISA resource of the real CS260 (default: from config, GPIB0::4::INSTR)")
    ap.add_argument("--config", default=None,
                    help="path to a .ini config to load (default: cs260.ini if present)")
    args = ap.parse_args()

    path = args.config or (DEFAULT_INI if os.path.exists(DEFAULT_INI) else None)
    cfg = Config.load(path) if path else Config()
    if path:
        print(f"config: {os.path.abspath(path)}")

    if args.real:
        from cs260.backends.cornerstone import CornerstoneGPIB
        resource = args.visa or cfg.hardware.visa
        backend = CornerstoneGPIB(resource,
                                  timeout_ms=cfg.hardware.timeout_ms,
                                  move_timeout_ms=cfg.hardware.move_timeout_ms,
                                  arrive_tol_nm=cfg.motion.arrive_tol_nm,
                                  n_gratings=cfg.gratings.count,
                                  filter_wheel=cfg.accessories.filter_wheel,
                                  dual_port=cfg.accessories.dual_port)
        mono = Monochromator(backend, cfg)
        print(f"REAL backend -> {resource}")
    else:
        mono, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = Cs260Service(mono, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except HardwareBusy as exc:
        # Another service already drives this GPIB address (hwlock). Say so in
        # ONE line -- the launcher shows it in its log -- and exit non-zero.
        # No shutdown here on purpose: the claim failed BEFORE open() sent a
        # byte, so the CS260 belongs to the other service, and closing "our"
        # shutter would close THEIRS. (serve_forever() only calls stop() once
        # start() has succeeded.) ASCII message: gotcha #14.
        print(f"cs260: cannot start: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
