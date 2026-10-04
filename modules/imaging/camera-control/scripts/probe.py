"""List the IDS cameras this PC can see, as ONE JSON line.

    uv run scripts/probe.py

Mission Control's "Instruments on this PC" runs this in camera-control's own
environment (module.toml [hardware] probe). It opens no camera: see
src/camera/probe.py. Output is ASCII (gotcha #14); exit 0 also when nothing
is found -- the "note" says why.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    try:
        from camera.probe import probe
        result = probe()
    except Exception as exc:                     # a probe never crashes the scan
        result = {"devices": [], "note": f"camera probe failed: {type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
