"""viewer.py -- start the AaltoView from scan-core.

The viewer is its own project now: https://github.com/FlashLukas/AaltoView
(installed into scan-core's environment as a dependency). This file stays so the
launcher's "Data viewer" button and `uv run python apps/viewer.py` keep working.

    uv run python apps/viewer.py [file.nc] [--folder DIR] [--theme light]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # find scan_core

from aaltoview.apps.viewer import (ViewerWidget, ViewerWindow,  # noqa: E402,F401
                                    configure_pyqtgraph, main)

if __name__ == "__main__":
    raise SystemExit(main())
