"""Run the DynaCool (ppms) control service -- simulated (default) or real (--real).

    uv run scripts/run_service.py                       # simulated DynaCool
    uv run --extra real scripts/run_service.py --real   # MultiVu, via MultiPyVu
    uv run --extra real scripts/run_service.py --real --scaffold
                                                        # real code path, MultiPyVu's
                                                        # own simulation (no MultiVu)

The service owns the cryostat brain and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)
Default ports 5579 / 5580. Drive it with:
    uv run scripts/ppms_console.py

--real needs MultiVu RUNNING on this PC (it owns the DynaCool) and the `real`
extra installed:  uv sync --extra gui --extra real
Starting or stopping the service never changes the field or the temperature.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from ppms.config import Config
from ppms.cryostat import Cryostat
from ppms.sim_system import build_sim_system
from ppms.net.service import PpmsService
from ppms.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="Quantum Design DynaCool control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real DynaCool through MultiVu (needs the `real` extra)")
    ap.add_argument("--scaffold", action="store_true",
                    help="with --real: MultiPyVu's own simulation, no MultiVu needed")
    ap.add_argument("--flavor", default=None,
                    help="MultiVu flavor (default: from config, DYNACOOL)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use ppms.ini in the project folder if this PC saved one
    # (e.g. the magnet's real field limit, or tolerances), as camera does.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "ppms.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"ppms service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.flavor is not None:
        cfg.hardware.flavor = args.flavor
    if args.scaffold:
        cfg.hardware.scaffolding = True

    if args.real:
        from ppms.backends.multivu import MultiVuDynaCool
        cryo = Cryostat(MultiVuDynaCool(cfg), cfg)
        how = " (MultiPyVu scaffolding)" if cfg.hardware.scaffolding else ""
        print(f"REAL backend -> MultiVu {cfg.hardware.flavor}{how}")
    else:
        cryo, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = PpmsService(cryo, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except RuntimeError as exc:
        # most often: MultiVu not running, or the `real` extra not installed
        print(f"could not start: {exc}")
        if args.real:
            print("check: MultiVu is running on this PC; the extra is installed "
                  "(uv sync --extra gui --extra real)")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
