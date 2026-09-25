"""Run the VNA service -- simulated (default) or a real analyser (--real).

    uv run scripts/run_service.py                          # simulator, field from mag2d on this PC
    uv run scripts/run_service.py --field manual --field-mT 80 --angle-deg 45
    uv run scripts/run_service.py --field clMag --clmag-host 192.168.1.42
    uv run --extra real scripts/run_service.py --real      # the PNA-X ("N5222A" VISA alias)
    uv run --extra real scripts/run_service.py --real --visa "TCPIP0::10.0.0.5::hislip0::INSTR"
    uv run --extra real scripts/run_service.py --real --driver cmt --field ppms
                                                           # Copper Mountain C1209 (S2VNA on
                                                           # this PC) + the DynaCool's field

Two real analysers: --driver pna (Keysight PNA-X, the default) or --driver cmt
(Copper Mountain C1209). The launcher only passes --real, so a PC keeps its
choice in vna.ini next to this folder's pyproject.toml (not in git), e.g.
    [hardware]
    driver = cmt
    [field]
    source = ppms

The service exposes the analyser over ZeroMQ:
  * commands on tcp://0.0.0.0:5573   (REP)
  * status   on tcp://0.0.0.0:5574   (PUB, 10 Hz)

The field is READ from a magnet service's status stream -- mag2d (port 5576) by
default, clMag (5556) with --field clMag, the DynaCool (5580) with --field
ppms -- in BOTH modes: the real analyser
files it with every sample, the simulator also uses it for the physics. When the
launcher starts the service, AALTOFLOW_ENDPOINTS says where those magnets really
listen, so a port changed in the launcher reaches the VNA too. Start order does
not matter: until the magnet is heard, the VNA uses the manual field and reports
field_ok = False.

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

from vna.config import Config
from vna.field import FIELD_SOURCES
from vna.net.service import VnaService
from vna.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def apply_launcher_endpoints(cfg: Config) -> None:
    """Point the field subscriptions at the magnets the launcher is actually running.

    Both magnet keys are applied when present, whichever source is selected:
    the source can be switched at run time, and it should then find the right
    port. Only what the launcher passed is overridden; started by hand (no
    variable), the config is used as it is.
    """
    # AALTOFLOW_ENDPOINTS since the 2026-09-24 rename; the old name still works
    raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
    if not raw:
        return
    try:
        eps = json.loads(raw)
        if not isinstance(eps, dict):
            raise ValueError
    except ValueError:
        print("vna service: AALTOFLOW_ENDPOINTS is not valid JSON, ignored")
        return
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
        print(f"vna service: {key} status at {host}:{pub} (from the launcher)")


def build_analyzer(cfg: Config, real: bool):
    """The brain on the chosen backend. Imports stay inside, so the simulator
    path never touches the real backends' modules and vice versa."""
    if real:
        from vna.analyzer import Analyzer
        from vna.backends import real_backend
        return Analyzer(real_backend(cfg), cfg)
    from vna.sim_system import build_sim_system
    return build_sim_system(cfg)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="VNA service: a real analyser (--real) or simulator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real analyser (--driver) instead of simulating")
    ap.add_argument("--driver", choices=["pna", "cmt"], default=None,
                    help="which real analyser: pna = Keysight PNA-X, cmt = Copper Mountain "
                         "C1209 (default: from config, pna)")
    ap.add_argument("--visa", default=None,
                    help="VISA resource/alias of the analyser (PNA default: N5222A; "
                         "C1209 default: TCPIP0::127.0.0.1::5025::SOCKET)")
    ap.add_argument("--cal-set", default=None, help="calibration set to activate on connect")
    ap.add_argument("--field", choices=list(FIELD_SOURCES), default=None,
                    help="where the field comes from (default: from config, mag2d)")
    ap.add_argument("--field-mT", type=float, default=None, help="the manual field")
    ap.add_argument("--angle-deg", type=float, default=None, help="the manual field angle")
    ap.add_argument("--mag2d-host", default=None, help="host of the vector magnet service")
    ap.add_argument("--mag2d-pub-port", type=int, default=None)
    ap.add_argument("--mag2dcal-host", default=None,
                    help="host of the calibrated vector magnet service")
    ap.add_argument("--mag2dcal-pub-port", type=int, default=None)
    ap.add_argument("--clmag-host", default=None, help="host of the 1-axis magnet service")
    ap.add_argument("--clmag-pub-port", type=int, default=None)
    ap.add_argument("--ppms-host", default=None, help="host of the DynaCool (ppms) service")
    ap.add_argument("--ppms-pub-port", type=int, default=None)
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    # No --config: use vna.ini in the project folder if this PC has saved one.
    # That is where a PC keeps what the launcher cannot pass -- on the
    # DynaCool setup:  [hardware] driver = cmt   and   [field] source = ppms.
    config = args.config
    default_ini = Path(__file__).resolve().parents[1] / "vna.ini"
    if config is None and default_ini.is_file():
        config = str(default_ini)
        print(f"vna service: settings from {default_ini.name}")
    cfg = Config.load(config) if config else Config()
    apply_launcher_endpoints(cfg)
    if args.field:
        cfg.field.source = args.field
    if args.field_mT is not None:
        cfg.field.manual_mT = args.field_mT
    if args.angle_deg is not None:
        cfg.field.manual_angle_deg = args.angle_deg
    if args.mag2d_host:
        cfg.field.mag2d_host = args.mag2d_host
    if args.mag2d_pub_port:
        cfg.field.mag2d_pub_port = args.mag2d_pub_port
    if args.mag2dcal_host:
        cfg.field.mag2dcal_host = args.mag2dcal_host
    if args.mag2dcal_pub_port:
        cfg.field.mag2dcal_pub_port = args.mag2dcal_pub_port
    if args.clmag_host:
        cfg.field.clMag_host = args.clmag_host
    if args.clmag_pub_port:
        cfg.field.clMag_pub_port = args.clmag_pub_port
    if args.ppms_host:
        cfg.field.ppms_host = args.ppms_host
    if args.ppms_pub_port:
        cfg.field.ppms_pub_port = args.ppms_pub_port
    if args.driver:
        cfg.hardware.driver = args.driver
    if args.visa:
        if cfg.hardware.driver == "cmt":
            cfg.hardware.cmt_resource = args.visa
        else:
            cfg.hardware.visa_resource = args.visa
    if args.cal_set is not None:
        cfg.hardware.cal_set = args.cal_set

    vna = build_analyzer(cfg, args.real)
    f = cfg.field
    where = {"mag2d": f"mag2d at {f.mag2d_host}:{f.mag2d_pub_port}",
             "mag2dcal": f"mag2dcal at {f.mag2dcal_host}:{f.mag2dcal_pub_port}",
             "clMag": f"clMag at {f.clMag_host}:{f.clMag_pub_port}",
             "ppms": f"ppms at {f.ppms_host}:{f.ppms_pub_port}"}.get(
        f.source, f"manual {f.manual_mT:g} mT at {f.manual_angle_deg:g} deg")
    hw = cfg.hardware
    if args.real and hw.driver == "cmt":
        print(f"REAL VNA: Copper Mountain C1209 via S2VNA at '{hw.cmt_resource}' "
              f"-- field from {where}")
    elif args.real:
        print(f"REAL VNA: Keysight PNA-X at VISA '{hw.visa_resource}' -- field from {where}")
    else:
        print(f"SIMULATED VNA -- field from {where}")
    try:
        VnaService(vna, host=args.host, cmd_port=args.cmd_port,
                   pub_port=args.pub_port).serve_forever()
    except Exception as exc:
        # Opening the analyser happens in serve_forever -> start. Say plainly
        # what went wrong (VISA alias not found, pyvisa missing, instrument
        # off) rather than dying with a traceback in the launcher log.
        print(f"vna service: could not start: {type(exc).__name__}: {exc}")
        if args.real and hw.driver == "cmt":
            print("  check: S2VNA is running with the C1209 connected, its socket server "
                  "is ON (System > Misc Setup > Network Setup > Socket Server, port 5025), "
                  f"the address '{hw.cmt_resource}' matches, and the real extra is "
                  "installed (uv sync --extra gui --extra real)")
        elif args.real:
            print("  check: the PNA-X is on and reachable, the VISA alias/address "
                  f"'{cfg.hardware.visa_resource}' exists (Keysight Connection Expert), "
                  "and pyvisa is installed (uv sync --extra gui --extra real)")
        try:
            vna.shutdown()
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
