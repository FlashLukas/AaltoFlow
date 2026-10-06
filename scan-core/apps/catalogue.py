"""catalogue.py -- the run catalogue in a window of its own.

Mission Control's "Catalogue" button opens this (Lukas, 2026-10-05: "mission
control should have a button for a database"): search every run in the data
folder without opening the measurement suite. It is the same widget as the
suite's Catalogue tab (apps/catalogue_view.py); a double-click opens the run
in the data viewer (apps/viewer.py, a separate window).

    uv run python apps/catalogue.py [--folder DIR] [--theme light]

The folder is the one the measurement suite saves into (remembered in
suite_local.json, set on its Settings tab), unless --folder says otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # find scan_core

from PySide6 import QtWidgets  # noqa: E402

from suite_common import get_setting, title as suite_title  # noqa: E402

from apps.theme import DEFAULT_THEME, apply, set_theme  # noqa: E402

VIEWER = Path(__file__).resolve().parent / "viewer.py"


def default_folder() -> Path:
    """The measurement suite's data folder, as the suite resolves it."""
    return Path(get_setting("data_dir")
                or Path(__file__).resolve().parent.parent / "out")


def open_in_viewer(path) -> subprocess.Popen | None:
    """Open one file in the data viewer: in the viewer window that is already
    running if there is one (it loads the file and comes to the front, ~0.2 s),
    else in a new viewer process (this one does not wait for it; starting a
    viewer takes seconds). Returns the new process, or None when handed over.

    The handover (aaltoview.apps.single_instance) imports only QtCore +
    QtNetwork and gives up after 0.5 s in total, so it runs on the GUI thread:
    when no viewer listens it fails at once."""
    try:
        from aaltoview.apps.single_instance import send_to_running
        if send_to_running(path):
            return None
    except Exception:                    # an older aaltoview: start one as before
        pass
    try:
        return subprocess.Popen([sys.executable, str(VIEWER), str(path)])
    except OSError:
        return None


class CatalogueWindow(QtWidgets.QMainWindow):
    def __init__(self, folder=None, open_file=None):
        super().__init__()
        from apps.catalogue_view import CatalogueWidget
        self.setWindowTitle(suite_title("Catalogue"))
        self.resize(1400, 860)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(110)
        self.catalogue = CatalogueWidget(data_dir=Path(folder or default_folder()),
                                         open_file=open_file or open_in_viewer,
                                         on_log=self.log_box.appendPlainText)
        root = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(root)
        v.setContentsMargins(14, 12, 14, 12)
        v.addWidget(self.catalogue, 1)
        v.addWidget(self.log_box)
        self.setCentralWidget(root)

    def closeEvent(self, event):
        # a rescan still running in its thread: let it stop before the window goes
        try:
            self.catalogue._cancel.set()
        except Exception:
            pass
        super().closeEvent(event)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AaltoFlow run catalogue")
    ap.add_argument("--folder", default=None, help="data folder to search "
                    "(default: the measurement suite's)")
    ap.add_argument("--theme", choices=["dark", "light"], default=None)
    args = ap.parse_args(argv)
    set_theme(args.theme or DEFAULT_THEME)        # BEFORE any widget is built
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apps.theme import CATALOGUE_ICON, apply_window_icon
    # its own icon and taskbar button: not grouped with the measurement suite
    apply_window_icon(app, CATALOGUE_ICON, "Aalto.AaltoFlow.catalogue")
    apply(app)
    win = CatalogueWindow(args.folder)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
