"""Run the spectrum-analyser service -- simulated (default) or a real Signal Hound (--real).

    uv run scripts/run_service.py                         # simulated SA44B + TG44A
    uv run scripts/run_service.py --model SA124B          # simulate the 12.4 GHz one
    uv run scripts/run_service.py --real                  # the first Signal Hound found
    uv run scripts/run_service.py --real --serial 12345678 --model SA124B
    uv run scripts/run_service.py --real --dll "C:\\Program Files\\Signal Hound\\sa_api.dll"

The real analyser needs Signal Hound's sa_api.dll (it comes with the Spike
software / the SDK) on the PATH or given with --dll; nothing is pip-installed
for it. The launcher only passes --real, so a PC keeps its choice of analyser
in signalhound.ini next to this folder's pyproject.toml (not in git), e.g.
    [hardware]
    model = SA124B
    serial = 12345678
    dll_path = C:\\Program Files\\Signal Hound\\Spike\\sa_api.dll

The service exposes the analyser over ZeroMQ:
  * commands on tcp://0.0.0.0:5587   (REP)
  * status   on tcp://0.0.0.0:5588   (PUB, 10 Hz)

Start-up writes nothing: the analyser is opened and asked what it is (model,
serial, TG present) and left idle -- no saved setting is sent until a setter,
"continuous on" or an acquire asks for a sweep (acquisition.sweep_on_start =
true restores sweeping at start). The tracking generator is never switched on
at start, and the analyser is aborted and closed (TG output off) on shutdown,
Ctrl+C or a crash.

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

from signalhound.config import Config
from signalhound.net.service import SignalhoundService
from signalhound.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def build_analyzer(cfg: Config, real: bool):
    """The brain on the chosen backend. Imports stay inside, so the simulator
    path never touches the real backend's module and vice versa."""
    if real:
        from signalhound.spectrum import SpectrumAnalyzer
        from signalhound.backends import real_backend
        return SpectrumAnalyzer(real_backend(cfg), cfg)
    from signalhound.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Spectrum analyser service: a real Signal Hound "
                                             "(--real) or a simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real analyser through sa_api.dll instead of simulating")
    ap.add_argument("--model", choices=["auto", "SA44B", "SA124B"], default=None,
                    help="expected model (real: refuse another one; sim: which to simulate)")
    ap.add_argument("--serial", type=int, default=None,
                    help="open the analyser with this serial number (default: the first)")
    ap.add_argument("--dll", default=None, help="full path to sa_api.dll")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use signalhound.ini in the project folder if this PC has saved one.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "signalhound.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"signalhound service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.model:
        cfg.hardware.model = args.model
    if args.serial is not None:
        cfg.hardware.serial = args.serial
    if args.dll:
        cfg.hardware.dll_path = args.dll

    signalhound = build_analyzer(cfg, args.real)
    hw = cfg.hardware
    if args.real:
        which = f"serial {hw.serial}" if hw.serial else "the first one found"
        print(f"REAL Signal Hound ({hw.model}, {which}) via "
              f"{hw.dll_path or 'sa_api.dll on the PATH'}")
    else:
        print("SIMULATED Signal Hound " + (hw.model if hw.model != "auto" else "SA44B")
              + " with a tracking generator")
    from signalhound.hwlock import HardwareBusy
    try:
        SignalhoundService(signalhound, host=args.host, cmd_port=args.cmd_port,
                           pub_port=args.pub_port).serve_forever()
    except HardwareBusy as exc:
        # Another service already drives THIS analyser (same serial number).
        # One clean line naming the holder, no traceback. Nothing to shut
        # down: the backend never opened the device (or closed its handle
        # without sending anything), so no abort/"safe state" is sent to a box
        # that belongs to someone else.
        print(f"signalhound service: could not start: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        # Opening the analyser happens in serve_forever -> start. Say plainly
        # what went wrong (DLL not found, no analyser plugged in, wrong model)
        # rather than dying with a traceback in the launcher log.
        print(f"signalhound service: could not start: {type(exc).__name__}: {exc}")
        if args.real:
            print("  check: the analyser is plugged in (USB), Spike is NOT running (it "
                  "would hold the device), sa_api.dll is on the PATH or in "
                  "hardware.dll_path, and hardware.model / serial match the unit")
        try:
            signalhound.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
