"""Start the SCAN SERVER: scans run in this process, any measurement suite watches.

    python scripts/run_scan_server.py                  # on 5551/5552, following the launcher
    python scripts/run_scan_server.py --sim            # on scan-core's simulated registry
    python scripts/run_scan_server.py --cmd-port 27000 --pub-port 27001

Mission Control starts it from the "Scan server" card (scan-core/module.toml)
with scan-core's own .venv python -- no `uv run` wrapper, so Stop really stops
it (gotcha #7). It binds 0.0.0.0: a suite in the office reaches it over the
lab network exactly as it reaches an instrument module. What it does and the
rules (who may start, abort, answer): scan_core/scan_server.py and
docs/DEVELOPER_NOTES.md, "The scan server".

Exit codes: 2 = a port is taken (another scan server, or an orphan),
3 = the lab's security policy secures it but this PC has no key.
"""

from __future__ import annotations

import sys
from pathlib import Path

# run straight from a checkout: scan-core's root holds the scan_core package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan_core.scan_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
