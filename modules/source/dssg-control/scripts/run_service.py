"""Run the SG12000L control service.

    uv run scripts/run_service.py                          # simulated generator
    uv run scripts/run_service.py --real                   # real unit, transport from config
    uv run scripts/run_service.py --real --com COM7        # real unit over USB
    uv run scripts/run_service.py --real --ip 10.0.0.23    # real unit over Ethernet
    uv run scripts/run_service.py --cmd-port 5591 --pub-port 5592

The service owns the generator and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

If a `dssg.ini` sits in the project folder it is loaded automatically (or
pass --config); so is the unit's measured power calibration,
`dssg_power_calibration.json` (scripts/calibrate_power.py writes it). At
start the service READS the unit's state (RF on/off, frequency, power, phase,
reference) and adopts it -- it changes nothing. The RF output is switched OFF
when the service stops.
Drive it with:
    uv run scripts/dssg_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dssg import vernier_cal
from dssg.config import Config
from dssg.hwlock import HardwareBusy
from dssg.synthesizer import Synthesizer
from dssg.sim_system import build_sim_system, build_real_backend
from dssg.net.service import DssgService, PortInUse
from dssg.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def main() -> int:
    ap = argparse.ArgumentParser(description="DS Instruments SG12000L control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real SG12000L (USB needs the `real` extra); default is simulated")
    ap.add_argument("--com", default=None,
                    help="real unit over USB on this COM port (sets transport=serial)")
    ap.add_argument("--ip", "--device-ip", dest="ip", default=None,
                    help="real unit over Ethernet at this IP address (sets transport=tcp)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    default_ini = os.path.join(_HERE, "..", "dssg.ini")
    ini = args.config or (default_ini if os.path.isfile(default_ini) else None)
    cfg = Config.load(ini) if ini else Config()
    if ini:
        print(f"config: {os.path.abspath(ini)}")

    if args.real:
        if args.com:
            cfg.hardware.transport, cfg.hardware.com_port = "serial", args.com
        if args.ip:
            cfg.hardware.transport, cfg.hardware.host = "tcp", args.ip
        synth = Synthesizer(build_real_backend(cfg), cfg)
        hw = cfg.hardware
        where = hw.com_port if hw.transport == "serial" else f"{hw.host}:{hw.tcp_port}"
        print(f"REAL backend -> SG12000L via {hw.transport} {where}")
    else:
        synth, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    # The POWER CALIBRATION file (hardware.power_calibration, default
    # dssg_power_calibration.json) is looked up next to dssg.ini, in the module
    # folder, when its path is relative. Only the service does this: tests and
    # a GUI's private simulation leave calibration_dir unset and never pick up
    # a unit's calibration by accident.
    synth.calibration_dir = os.path.abspath(os.path.join(_HERE, ".."))
    # ...and SAID on stdout (the launcher's log), not only as an event: which
    # file, when it was measured, its range, passes and worst spread -- or
    # that there is none.
    print(vernier_cal.summary_line(synth.calibration_path(),
                                   bool(cfg.hardware.fine_power)))

    service = DssgService(synth, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    try:
        service.serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"dssg service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (another dssg, or any module pointed at this COM
        # port / IP) already holds the unit. We never opened it, so there is
        # nothing to switch off or close: say who holds it, in one line, exit.
        print(f"dssg: cannot start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
