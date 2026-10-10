"""Run the oscilloscope service -- simulated (default) or the real scope (--real).

    uv run scripts/run_service.py                       # the simulated bench (two test signals)
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


def build_scope(cfg: Config, real: bool, gen_cfg=None):
    """The brain on the simulator, or on the real instrument chosen by
    hardware.driver (siglent / dwf; its library imported only then)."""
    if real:
        from scope.real_system import build_real_system
        return build_real_system(cfg, gen_cfg)
    from scope.sim_system import build_sim_system
    return build_sim_system(cfg, gen_cfg=gen_cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Oscilloscope service: real (--real) or simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real scope (pyvisa + a VISA library) instead of simulating")
    ap.add_argument("--visa", default=None, help="VISA resource of the real scope")
    ap.add_argument("--driver", choices=["siglent", "dwf"], default="siglent",
                    help="which instrument: siglent (default) or dwf (an Analog Discovery). "
                         "Chooses the settings file too: scope.ini / scope-dwf.ini")
    ap.add_argument("--dwf-device", default=None,
                    help="dwf: the Analog Discovery's serial (or #n); default the first free")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # ONE SETTINGS FILE PER INSTRUMENT. The Siglent and the Analog Discovery
    # are two services of this module, each with its own settings: scope.ini
    # (siglent) and scope-dwf.ini (dwf), next to the project; --config names
    # another. The DRIVER comes from the command line only -- lab PC
    # 2026-10-10: `--driver dwf` was saved into the one shared scope.ini, and
    # the next plain (Siglent) start opened the Analog Discovery.
    config = args.config
    project = Path(__file__).resolve().parents[1]
    default_ini = project / ("scope.ini" if args.driver == "siglent" else
                             f"scope-{args.driver}.ini")
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"scope service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    cfg.hardware.driver = args.driver
    if args.visa:
        cfg.hardware.visa = args.visa
    if args.dwf_device is not None:
        cfg.hardware.dwf_device = args.dwf_device
    # The GENERATOR's settings (an instrument with one: the Analog Discovery's
    # W1/W2 -- coupling, limits) live in their own file next to the settings:
    # scope-dwf-generator.ini (one saved as scope-generator.ini before
    # 2026-10-10 is read once, if the new one does not exist yet).
    from scope.generator.config import GenConfig
    gen_ini = Path(config or default_ini)
    gen_ini = gen_ini.with_name(gen_ini.stem + "-generator.ini")
    old_gen = project / "scope-generator.ini"
    src = gen_ini if gen_ini.is_file() else (old_gen if old_gen.is_file() else None)
    gen_cfg = GenConfig.load(str(src)) if src else GenConfig()

    spec = build_scope(cfg, args.real, gen_cfg)
    if spec.gen is not None:
        spec.gen.persist_path = str(gen_ini)
    # The module's settings (physical units = a probe's calibration, averaging,
    # filter, loop) are saved to scope.ini at every change, so a restart keeps
    # them (Lukas, 2026-10-07: "need to be saved"). A --config file given on
    # the command line is the one written.
    spec.persist_path = config or str(default_ini)
    if args.real and cfg.hardware.driver == "dwf":
        print("REAL Analog Discovery (dwf) "
              + (f"'{cfg.hardware.dwf_device}'" if cfg.hardware.dwf_device else "(first free)"))
    elif args.real:
        print(f"REAL scope at {cfg.hardware.visa}")
    elif cfg.sim.model == "ad":
        print("SIMULATED Analog Discovery (W1/W2 looped back to CH1/CH2, V+/V-)")
    else:
        print("SIMULATED scope (two test signals + a sync square)")
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
