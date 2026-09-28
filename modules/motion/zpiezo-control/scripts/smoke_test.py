"""Instant offline sanity check for the Z-piezo module."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zpiezo.config import Config          # noqa: E402
from zpiezo.sim_system import build_sim_system  # noqa: E402


def main() -> None:
    brain, backend = build_sim_system(Config())
    brain.start()
    print("set 7.5 V ->", brain.set_voltage(7.5))
    print("read      ->", brain.read_voltage())
    print("clamp 999 ->", brain.set_voltage(999), "(limit", brain.cfg.limits.v_max, ")")
    print("status    ->", brain.status())
    brain.shutdown()
    print("smoke test OK")


if __name__ == "__main__":
    main()
