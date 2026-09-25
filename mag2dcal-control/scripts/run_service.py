"""Run the calibrated vector-magnet control service.

    uv run scripts/run_service.py                    # simulated magnet
    uv run scripts/run_service.py --real             # the NI DAQ (needs nidaqmx)
    uv run scripts/run_service.py --bypass-water     # run without the water interlock (DANGER)
    uv run scripts/run_service.py --config mag2dcal.ini --cmd-port 5577 --pub-port 5578
    uv run scripts/run_service.py --calibration Calibrations\\mag2dcal_....json
    uv run scripts/run_service.py --no-calibration   # ignore saved curves, run on the
                                                     # straight line (and say so)

CALIBRATION. By default the newest *.json in the project's Calibrations folder
is loaded at start, and its measured range becomes the field limit. With none
the magnet still runs, using B / control.ff_mT_per_V for the jump, and warns
that it is uncalibrated. Measure one with the `calibrate` verb (the GUI's
Calibration card, or `mag2dcal_console.py calibrate`).

The service owns the magnet and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)

WATER CHECK AT START: if the cooling water is off and the interlock is not
bypassed, the service prints why and exits with code 3 -- before it opens a
socket, so nothing can command a magnet that must not run.

Stop it with Ctrl+C, the launcher, or the `shutdown` command: all three ramp the
output to 0 V at the slew rate and release the enable line. A hard kill cannot
(the DAQ keeps its last output) -- avoid taskkill.
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from mag2dcal.calibration import Calibration
from mag2dcal.config import Config
from mag2dcal.controller import Controller, WaterInterlockError
from mag2dcal.sim_system import build_sim_system
from mag2dcal.net.service import Mag2dcalService
from mag2dcal.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

EXIT_WATER = 3


def main() -> int:
    ap = argparse.ArgumentParser(description="mag2dcal vector magnet control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real magnet through NI-DAQmx; default is simulated")
    ap.add_argument("--bypass-water", action="store_true",
                    help="DANGER: run without the cooling-water interlock")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    ap.add_argument("--calibration", default=None, metavar="FILE",
                    help="load this calibration .json instead of the newest saved one")
    ap.add_argument("--no-calibration", action="store_true",
                    help="start uncalibrated (the jump uses ff_mT_per_V)")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()
    if args.bypass_water:
        cfg.interlock.water_bypass = True
        print("WARNING: water interlock BYPASSED for this run")
    if args.no_calibration:
        cfg.calibration.load_newest_on_start = False

    if args.real:
        try:
            from mag2dcal.backends.nidaq import NidaqVectorMagnet
            ctrl = Controller(NidaqVectorMagnet(cfg.hardware), cfg)
        except ImportError as exc:
            print(f"mag2dcal: cannot load the NI backend ({exc}). Install NI-DAQmx and "
                  "uncomment nidaqmx in pyproject.toml, then uv sync.")
            return 2
        print(f"REAL backend -> NI DAQ (AO {cfg.hardware.ao_x}, {cfg.hardware.ao_y})")
    else:
        ctrl, _ = build_sim_system(cfg)
        print("SIMULATED magnet (no hardware needed)")

    if args.calibration:
        # Set BEFORE start(), which is what would otherwise load the newest file.
        try:
            ctrl.set_calibration(Calibration.load(args.calibration))
            print(f"calibration from {args.calibration}")
        except Exception as exc:
            print(f"mag2dcal: cannot read {args.calibration}: "
                  f"{type(exc).__name__}: {exc}")
            return 2

    service = Mag2dcalService(ctrl, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except WaterInterlockError as exc:
        print("")
        print("mag2dcal: NOT STARTED -- cooling water interlock.")
        print(f"  {exc}")
        return EXIT_WATER
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
