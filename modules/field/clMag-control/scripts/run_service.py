"""Run the magnet control service (simulated hardware).

    uv run scripts/run_service.py                 # bind 0.0.0.0, default ports
    uv run scripts/run_service.py --cmd-port 5555 --pub-port 5556

The service owns the controller and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 10 Hz)

Point a GUI at it with:  uv run scripts/run_gui.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from clMag.config import Config
from clMag.sim_system import build_sim_system
from clMag.net.service import ClMagService, PortInUse
from clMag.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT
from clMag.hwlock import HardwareBusy

# Exit code when an instrument is already driven by another service. Distinct
# from 2 (bad arguments / --real not available) so a launcher can tell them apart.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="clMag magnet control service (simulated)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    # Every module's service accepts --real (the launcher passes it from the
    # card's "real" box). clMag's real Kepco + NI path is not wired into the
    # service yet, so say that plainly instead of silently running the sim.
    ap.add_argument("--real", action="store_true",
                    help="real hardware (not available yet for clMag)")
    args = ap.parse_args()
    if args.real:
        print("clMag: the real-hardware service is not implemented yet "
              "(only the simulator). Untick 'real' to run the simulated magnet.")
        return 2

    # The real system (not written yet) claims the Kepco's GPIB address and the
    # DAQ card through clMag.backends.claims BEFORE it opens anything; the sim
    # claims nothing. If another service already holds one of them, that claim
    # raises HardwareBusy. We end with ONE plain line on stderr (it names the
    # address and the holder, e.g. "GPIB0::6 is already in use by kepco (pid
    # 1234) ...") and no traceback, because a physicist reading the launcher log
    # needs the reason, not a stack.
    #
    # Safety: HardwareBusy comes out of the controller's start() (or before
    # it), i.e. BEFORE serve_forever's try/finally is entered, so stop() ->
    # controller.shutdown() -> "ramp to zero + OUTP OFF" is NOT run. That is
    # deliberate: we never opened the instrument, it belongs to the other
    # service, and switching its output off would sabotage that service's run.
    try:
        cfg = Config()
        ctrl, *_ = build_sim_system(cfg)
        service = ClMagService(ctrl, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"clMag service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as e:
        print(f"clMag: cannot start: {e}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
