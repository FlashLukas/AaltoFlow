"""Run the spectrometer service -- simulated (default) or the real CCS200 (--real).

    uv run scripts/run_service.py                          # simulator
    uv run scripts/run_service.py --real                   # the first CCS200 VISA can find
    uv run scripts/run_service.py --real --resource "USB0::0x1313::0x8089::M<serial>::RAW"

The service exposes the spectrometer over ZeroMQ:
  * commands on tcp://0.0.0.0:5603   (REP)
  * status   on tcp://0.0.0.0:5604   (PUB, 10 Hz)

The launcher only passes --real (and ports), so a PC keeps anything else --
the resource string, the DLL path, the user calibration -- in ccs200.ini next
to this folder's pyproject.toml (not in git), e.g.
    [hardware]
    resource = USB0::0x1313::0x8089::M<serial>::RAW

Close Thorlabs' ThorSpectra first: while it runs it holds the spectrometer.

Output is ASCII only: the launcher reads it through a pipe (suite gotcha #14).
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pathlib import Path

from ccs200.config import Config
from ccs200.hwlock import HardwareBusy
from ccs200.net.service import Ccs200Service
from ccs200.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def build_spectrometer(cfg: Config, real: bool):
    """The brain on the chosen backend. Imports stay inside, so the simulator
    path never touches the real backend's module and vice versa."""
    if real:
        from ccs200.spectrometer import Spectrometer
        from ccs200.backends import real_backend
        return Spectrometer(real_backend(cfg), cfg)
    from ccs200.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="CCS200 spectrometer service: real (--real) or simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real CCS200 (TLCCS_64.dll) instead of simulating")
    ap.add_argument("--resource", default=None,
                    help="VISA resource of the CCS200 (default: the first one found)")
    ap.add_argument("--dll", default=None, help="path to TLCCS_64.dll if not the default")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use ccs200.ini in the project folder if this PC has saved one.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "ccs200.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"ccs200 service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.resource:
        cfg.hardware.resource = args.resource
    if args.dll:
        cfg.hardware.dll_path = args.dll

    spec = build_spectrometer(cfg, args.real)
    if args.real:
        print(f"REAL spectrometer: Thorlabs CCS200 at "
              f"'{cfg.hardware.resource or 'first found'}'")
    else:
        print("SIMULATED spectrometer (lamp + Hg/Ar lines)")
    try:
        Ccs200Service(spec, host=args.host, cmd_port=args.cmd_port,
                      pub_port=args.pub_port).serve_forever()
    except HardwareBusy as exc:
        # Another service (this module or any other pointed at the same
        # spectrometer) already holds it. One plain line, no traceback: the
        # message already names the address and the holder. Nothing to close
        # or make safe -- we never opened the device, and open() released
        # whatever it had claimed.
        print(f"ccs200 service: could not start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        # Opening the instrument happens in serve_forever -> start. Say plainly
        # what went wrong (DLL missing, spectrometer busy, unplugged) rather
        # than dying with a traceback in the launcher log.
        print(f"ccs200 service: could not start: {type(exc).__name__}: {exc}")
        if args.real:
            print("  check: the CCS200 is plugged in, ThorSpectra is CLOSED, the "
                  "Thorlabs CCS driver is installed (TLCCS_64.dll), and the "
                  "resource string matches the unit")
        try:
            spec.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
