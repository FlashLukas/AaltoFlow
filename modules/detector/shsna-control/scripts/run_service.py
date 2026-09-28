"""Run the scalar network analyser service -- simulated (default) or real (--real).

    uv run scripts/run_service.py                          # standalone simulator
    uv run scripts/run_service.py --real                   # TG sweeps via the signalhound
                                                           # service on this PC (5587/5588)
    uv run scripts/run_service.py --real --owner-host 192.168.1.42

--real does NOT open any USB device: the analyser and its tracking generator
belong to the signalhound service (one process per analyser, the SA API's
rule), and this service asks it for TG sweeps. Start signalhound first (the
launcher does, from module.toml's start_after) -- but order is not critical:
until the owner is heard, status says so in hw_error and acquisitions fail
with that reason.

When the launcher starts this service, AALTOFLOW_ENDPOINTS says where the
signalhound service really listens, so a port changed in the launcher reaches
this module too.

The service exposes the analyser over ZeroMQ:
  * commands on tcp://0.0.0.0:5627   (REP)
  * status   on tcp://0.0.0.0:5628   (PUB, 10 Hz)

Output is ASCII only: the launcher reads it through a pipe (suite gotcha #14).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pathlib import Path

from shsna.config import Config
from shsna.hwlock import HardwareBusy
from shsna.net.service import ShsnaService, PortInUse
from shsna.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

# Exit code 4 = "this instrument is already in use by another service"
# (hwlock.py). The SAME number in every AaltoFlow module. This module claims no
# hardware itself (the owner does), so it is only here for the shared contract.
EXIT_HARDWARE_BUSY = 4

#: the launcher's key for the service that owns the analyser
OWNER_KEY = "signalhound"


def apply_launcher_endpoints(cfg: Config) -> None:
    """Point the real backend at the signalhound service the launcher runs.

    Only what the launcher passed is overridden; started by hand (no
    variable), the config is used as it is."""
    # AALTOFLOW_ENDPOINTS since the 2026-09-24 rename; the old name still works
    raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
    if not raw:
        return
    try:
        eps = json.loads(raw)
        if not isinstance(eps, dict):
            raise ValueError
    except ValueError:
        print("shsna service: AALTOFLOW_ENDPOINTS is not valid JSON, ignored")
        return
    ep = eps.get(OWNER_KEY)
    if ep and len(ep) == 3:
        host, cmd, pub = ep
        hw = cfg.hardware
        hw.owner_host = "127.0.0.1" if host == "localhost" else str(host)
        hw.owner_cmd_port, hw.owner_pub_port = int(cmd), int(pub)
        print(f"shsna service: signalhound at {host}:{cmd}/{pub} (from the launcher)")
    # The simulated film's field source (config `field`, 2026-09-28): every
    # magnet the launcher runs, whichever source is selected -- the source can
    # be switched at run time and should then find the right port.
    for key, host_attr, port_attr in (("mag2d", "mag2d_host", "mag2d_pub_port"),
                                      ("mag2dcal", "mag2dcal_host", "mag2dcal_pub_port"),
                                      ("clMag", "clMag_host", "clMag_pub_port"),
                                      ("ppms", "ppms_host", "ppms_pub_port")):
        ep = eps.get(key)
        if not ep or len(ep) != 3:
            continue
        host, _cmd, pub = ep
        setattr(cfg.field, host_attr, "127.0.0.1" if host == "localhost" else str(host))
        setattr(cfg.field, port_attr, int(pub))
        print(f"shsna service: {key} status at {host}:{pub} (from the launcher, for the "
              f"simulated film)")


def build_analyzer(cfg: Config, real: bool):
    """The brain on the chosen backend. Imports stay inside, so the simulator
    path never touches the real backend's module and vice versa."""
    if real:
        from shsna.analyzer import Analyzer
        from shsna.backends import real_backend
        return Analyzer(real_backend(cfg), cfg)
    from shsna.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="scalar network analyser service "
                                             "(Signal Hound TG via signalhound, or a simulator)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="measure through the signalhound service instead of simulating")
    ap.add_argument("--owner-host", default=None, help="host of the signalhound service")
    ap.add_argument("--owner-cmd-port", type=int, default=None)
    ap.add_argument("--owner-pub-port", type=int, default=None)
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use shsna.ini in the project folder if this PC has saved one.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "shsna.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"shsna service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    apply_launcher_endpoints(cfg)
    hw = cfg.hardware
    if args.owner_host:
        hw.owner_host = args.owner_host
    if args.owner_cmd_port:
        hw.owner_cmd_port = args.owner_cmd_port
    if args.owner_pub_port:
        hw.owner_pub_port = args.owner_pub_port

    shsna = build_analyzer(cfg, args.real)
    if args.real:
        print(f"REAL: TG sweeps via the signalhound service at {hw.owner_host}:"
              f"{hw.owner_cmd_port} (status {hw.owner_pub_port})")
    else:
        print("SIMULATED scalar network analyser (TG -> pad -> DUT -> analyser)")
    try:
        ShsnaService(shsna, host=args.host, cmd_port=args.cmd_port,
                     pub_port=args.pub_port).serve_forever()
    except PortInUse as exc:
        # The command or status port is taken (a second copy, or an orphan --
        # gotcha #7). Nothing was opened: the sockets are bound BEFORE the
        # backend (gotcha #39).
        print(f"shsna service: cannot start: {exc}", file=sys.stderr)
        return 2
    except HardwareBusy as exc:
        print(f"shsna service: {exc}", file=sys.stderr)
        return EXIT_HARDWARE_BUSY
    except Exception as exc:
        print(f"shsna service: could not start: {type(exc).__name__}: {exc}")
        try:
            shsna.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
