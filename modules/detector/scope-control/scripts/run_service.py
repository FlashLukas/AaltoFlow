"""Run the oscilloscope service -- simulated (default) or the real scope (--real).

    uv run scripts/run_service.py                       # the simulated bench (MOKE loop)
    uv run scripts/run_service.py --real --visa "USB0::0xF4EC::0xEE3A::<serial>::INSTR"
    uv run scripts/run_service.py --cmd-port 5633 --pub-port 5634

At start the service only READS the scope's settings (V/div, time/div,
trigger ...) and adopts them; nothing on the scope changes. The launcher
passes --real and --visa (Mission Control > Instruments on this PC); anything
else this PC wants to keep (units, averaging, filter) lives in scope.ini
next to this project (Settings > Save config).
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pathlib import Path

from scope.config import Config
from scope.hwlock import HardwareBusy
from scope.net.service import ScopeService, PortInUse
from scope.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def build_scope(cfg: Config, real: bool):
    """The brain on the simulator, or on the real Siglent backend (imported
    only then: the simulator path never touches pyvisa)."""
    from scope.scope import Scope
    if real:
        from scope.backends.siglent import SiglentSDS
        return Scope(SiglentSDS(cfg.hardware.visa, timeout_ms=cfg.hardware.timeout_ms), cfg)
    from scope.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Oscilloscope service: real (--real) or simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real scope (pyvisa + a VISA library) instead of simulating")
    ap.add_argument("--visa", default=None, help="VISA resource of the real scope")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use scope.ini in the project folder if this PC has saved one.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "scope.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"scope service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.visa:
        cfg.hardware.visa = args.visa

    spec = build_scope(cfg, args.real)
    if args.real:
        print(f"REAL scope at {cfg.hardware.visa}")
    else:
        print(f"SIMULATED scope (bench: {cfg.sim.scene})")
    try:
        ScopeService(spec, host=args.host, cmd_port=args.cmd_port,
                      pub_port=args.pub_port).serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"scope service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service (this module or any other pointed at the same
        # scope) already holds it. One plain line, no traceback: the
        # message already names the address and the holder. Nothing to close
        # or make safe -- we never opened the device, and open() released
        # whatever it had claimed.
        print(f"scope service: could not start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        # Opening the instrument happens in serve_forever -> start. Say plainly
        # what went wrong (no VISA, scope busy, unplugged) rather
        # than dying with a traceback in the launcher log.
        print(f"scope service: could not start: {type(exc).__name__}: {exc}")
        if args.real:
            print("  check: the scope is on and plugged in (USB or the USB-GPIB "
                  "adapter), NI-VISA is installed, `uv sync --extra gui --extra real` "
                  "was run, and the VISA address matches (NI MAX lists it)")
        try:
            spec.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
