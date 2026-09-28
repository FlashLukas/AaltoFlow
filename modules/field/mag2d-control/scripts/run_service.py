"""Run the vector-magnet control service.

    uv run scripts/run_service.py                    # simulated magnet
    uv run scripts/run_service.py --real             # the NI DAQ (needs nidaqmx)
    uv run scripts/run_service.py --bypass-water     # run without the water interlock (DANGER)
    uv run scripts/run_service.py --config mag2d.ini --cmd-port 5575 --pub-port 5576

The service owns the magnet and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)

ADOPT AT START: the service reads the magnet (enable line, drive, field) and
takes it over as it is -- it does not switch the output on or off by itself.

WATER CHECK AT START: if the cooling water is off and the interlock is not
bypassed, the service prints why and exits with code 3 -- its sockets are
closed again before it answers anything, so nothing can command a magnet that
must not run.

ONE CARD, ONE SERVICE: with --real the service claims the DAQ device (Dev1)
before it opens anything. If mag2d or mag2dcal already holds it, the service
prints one line naming the holder and exits with code 4, touching nothing.

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

from mag2d.config import Config
from mag2d.controller import Controller, WaterInterlockError
from mag2d.sim_system import build_sim_system
from mag2d.net.service import Mag2dService, PortInUse
from mag2d.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from mag2d.hwlock import HardwareBusy

EXIT_WATER = 3
# The DAQ card is already driven by another service (mag2d started twice, or
# mag2dcal on the same coils). Its own code so a launcher can tell it apart
# from 2 (no NI backend) and 3 (no water).
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="mag2d vector magnet control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real magnet through NI-DAQmx; default is simulated")
    ap.add_argument("--bypass-water", action="store_true",
                    help="DANGER: run without the cooling-water interlock")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()
    if args.bypass_water:
        cfg.interlock.water_bypass = True
        print("WARNING: water interlock BYPASSED for this run")

    if args.real:
        try:
            from mag2d.backends.nidaq import NidaqVectorMagnet
            ctrl = Controller(NidaqVectorMagnet(cfg.hardware), cfg)
        except ImportError as exc:
            print(f"mag2d: cannot load the NI backend ({exc}). Install NI-DAQmx and "
                  "uncomment nidaqmx in pyproject.toml, then uv sync.")
            return 2
        print(f"REAL backend -> NI DAQ (AO {cfg.hardware.ao_x}, {cfg.hardware.ao_y})")
    else:
        ctrl, _ = build_sim_system(cfg)
        print("SIMULATED magnet (no hardware needed)")

    service = Mag2dService(ctrl, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except WaterInterlockError as exc:
        print("")
        print("mag2d: NOT STARTED -- cooling water interlock.")
        print(f"  {exc}")
        return EXIT_WATER
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"mag2d service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # ONE line on stderr, no traceback: the reader needs the address and
        # the holder, e.g. "DEV1 is already in use by mag2dcal (pid 1234) ...".
        # Safety: this comes out of ctrl.start() -> backend.open(), i.e. from
        # service.start(), BEFORE serve_forever's try/finally -- so stop() ->
        # ctrl.shutdown() (ramp to 0 V, enable off) is NOT run, and the backend
        # never created a task. The coils belong to the other service; zeroing
        # them would wreck its measurement.
        print(f"mag2d: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
