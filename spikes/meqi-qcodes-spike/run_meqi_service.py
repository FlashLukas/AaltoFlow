"""Start the REAL meqi sim service (unchanged code) on given ports."""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(__file__))  # make `meqi` importable

from meqi.config import Config
from meqi.sim_system import build_sim_system
from meqi.net.service import MeqiService

ap = argparse.ArgumentParser()
ap.add_argument("--cmd-port", type=int, default=5555)
ap.add_argument("--pub-port", type=int, default=5556)
args = ap.parse_args()

cfg = Config()
ctrl, *_ = build_sim_system(cfg)      # controller wired to the simulator
MeqiService(ctrl, host="0.0.0.0",
            cmd_port=args.cmd_port, pub_port=args.pub_port).serve_forever()
