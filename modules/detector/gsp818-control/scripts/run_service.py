"""Run the spectrum analyser service -- simulated (default) or the real GSP-818 (--real).

    uv run scripts/run_service.py                          # simulator
    uv run --extra real scripts/run_service.py --real      # the GSP-818, found on USB
    uv run --extra real scripts/run_service.py --real --visa "USB0::0x2184::...::INSTR"

The launcher only passes --real (and ports), so a PC keeps what it cannot pass
in gsp818.ini next to this folder's pyproject.toml (not in git), e.g.
    [hardware]
    resource = TCPIP0::192.168.1.168::inst0::INSTR

The service exposes the analyser over ZeroMQ:
  * commands on tcp://0.0.0.0:5585   (REP)
  * status   on tcp://0.0.0.0:5586   (PUB, 10 Hz)

At start the analyser's settings (span, RBW, reference level, tracking
generator, ...) are READ and adopted; nothing is written to it. The tracking
generator is switched OFF when the service stops.
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

from gsp818.config import Config
from gsp818.hwlock import HardwareBusy
from gsp818.net.service import Gsp818Service, PortInUse
from gsp818.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module, so the launcher or a
# script can tell this case apart from other start failures by the number alone.
EXIT_HARDWARE_BUSY = 4


def build_analyzer(cfg: Config, real: bool):
    """The brain on the chosen backend. Imports stay inside, so the simulator
    path never touches the real backend's module and vice versa."""
    if real:
        from gsp818.analyzer import SpectrumAnalyzer
        from gsp818.backends import real_backend
        return SpectrumAnalyzer(real_backend(cfg), cfg)
    from gsp818.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Spectrum analyser service: the GSP-818 (--real) or a simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real GSP-818 instead of simulating")
    ap.add_argument("--visa", default=None,
                    help="VISA address of the analyser (default: search USB for a GSP-818)")
    ap.add_argument("--sweep-mode", choices=["wait", "single"], default=None,
                    help="how a fresh trace is obtained on the real one (default: wait)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use gsp818.ini in the project folder if this PC has saved one.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "gsp818.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"gsp818 service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    if args.visa:
        cfg.hardware.resource = args.visa
    if args.sweep_mode:
        cfg.hardware.sweep_mode = args.sweep_mode

    sa = build_analyzer(cfg, args.real)
    hw = cfg.hardware
    if args.real:
        print(f"REAL spectrum analyser: GW Instek GSP-818 at "
              f"'{hw.resource or 'first GSP-818 on USB'}', sweep mode {hw.sweep_mode}")
    else:
        print("SIMULATED spectrum analyser (GSP-818 model)")
    try:
        Gsp818Service(sa, host=args.host, cmd_port=args.cmd_port,
                      pub_port=args.pub_port).serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # instrument. One line in the launcher log and a non-zero exit, instead
        # of a deaf service that holds the instrument (gotcha #39).
        print(f"gsp818 service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        # Another service already drives this analyser (hwlock: one physical
        # instrument, one service). One clear line on stderr, no traceback.
        # Nothing to shut down: open() never reached the instrument, and the
        # backend's close() sends nothing when it holds no session -- so the
        # "TG off" of a normal stop does NOT go to a box somebody else owns.
        print(f"gsp818 service: not started: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        # Opening the analyser happens in serve_forever -> start. Say plainly
        # what went wrong rather than dying with a traceback in the launcher log.
        print(f"gsp818 service: could not start: {type(exc).__name__}: {exc}")
        if args.real:
            print("  check: the GSP-818 is on and connected, a VISA library with a USB "
                  "driver is installed (NI-VISA), NI MAX lists it, and the real extra is "
                  "installed (uv sync --extra gui --extra real)")
        try:
            sa.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
