"""Run the phase-shifter control service.

    uv run scripts/run_service.py                       # simulated PS6000L
    uv run scripts/run_service.py --real                # the real unit (needs --extra real)
    uv run scripts/run_service.py --real --port COM7
    uv run scripts/run_service.py --cmd-port 5589 --pub-port 5590

The service owns the phase shifter and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:<cmd-port>   (REP)
  * status   on tcp://0.0.0.0:<pub-port>   (PUB, 5 Hz)

Drive it with:
    uv run scripts/dsphase_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dsphase.config import Config
from dsphase.shifter import PhaseShifter
from dsphase.sim_system import build_sim_system
from dsphase.net.service import DsphaseService
from dsphase.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT

DEFAULT_INI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dsphase.ini"))


def main() -> int:
    ap = argparse.ArgumentParser(description="DS Instruments phase shifter control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real phase shifter over USB (needs pyserial); default is simulated")
    ap.add_argument("--port", default=None,
                    help="COM port of the real unit (default: from config)")
    ap.add_argument("--config", default=None,
                    help="path to a .ini config to load (default: dsphase.ini in the "
                         "project folder, if it exists)")
    args = ap.parse_args()

    # The launcher starts us with no --config, so the lab PC's settings (above
    # all the COM port) must come from a file in a known place: dsphase.ini next
    # to pyproject.toml. The installer keeps *.ini files across upgrades.
    path = args.config
    if path is None and os.path.isfile(DEFAULT_INI):
        path = DEFAULT_INI
    cfg = Config.load(path) if path else Config()
    if path:
        print(f"config: {path}")

    if args.real:
        from dsphase.backends.ps6000l import PS6000L
        port = args.port or cfg.hardware.port
        backend = PS6000L(port, baud=cfg.hardware.baud,
                          timeout_s=cfg.hardware.timeout_s,
                          freq_command=cfg.device.freq_command)
        brain = PhaseShifter(backend, cfg)
        print(f"REAL backend -> {port}")
    else:
        brain, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    service = DsphaseService(brain, host=args.host, cmd_port=args.cmd_port, pub_port=args.pub_port)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
