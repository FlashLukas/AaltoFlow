"""Run the HF2LI lock-in control service.

    uv run scripts/run_service.py                          # simulated lock-in
    uv run scripts/run_service.py --real --device dev1234  # the real HF2LI (needs LabOne + zhinst-core)
    uv run scripts/run_service.py --config hf2.ini

The service owns the lock-in and exposes it over ZeroMQ:
  * commands on tcp://0.0.0.0:5569   (REP)
  * status   on tcp://0.0.0.0:5570   (PUB, 10 Hz)

Drive it with:
    uv run scripts/hf2_console.py --connect <host>
"""

from __future__ import annotations

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from hf2.config import Config
from hf2.lockin import LockIn
from hf2.sim_system import build_sim_system
from hf2.net.service import Hf2Service
from hf2.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser(description="HF2LI lock-in control service")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    ap.add_argument("--real", action="store_true",
                    help="drive the real HF2LI via LabOne (needs zhinst-core); default is simulated")
    ap.add_argument("--device", default=None,
                    help="device id of the real HF2LI, e.g. dev1234 (default: from config)")
    ap.add_argument("--config", default=None, help="path to a .ini config to load")
    args = ap.parse_args()

    cfg = Config.load(args.config) if args.config else Config()

    if args.real:
        from hf2.backends.zhinst_hf2 import ZhinstHF2
        hw = cfg.hardware
        if args.device:
            hw.device_id = args.device
        backend = ZhinstHF2(hw.device_id, host=hw.server_host, port=hw.server_port,
                            api_level=hw.api_level, interface=hw.interface)
        lockin = LockIn(backend, cfg)
        print(f"REAL backend -> {hw.device_id} via {hw.server_host}:{hw.server_port}")
    else:
        lockin, _ = build_sim_system(cfg)
        print("SIMULATED backend (no hardware needed)")

    Hf2Service(lockin, host=args.host, cmd_port=args.cmd_port,
               pub_port=args.pub_port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
