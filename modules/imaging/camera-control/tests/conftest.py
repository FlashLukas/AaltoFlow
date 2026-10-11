"""Make the src/ layout importable during tests without installing."""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")
# ... and the camera's live-pattern autosave goes to a temp folder: a test run
# on the lab PC must never overwrite (or restore) the rig's pattern.
_os.environ["AALTOFLOW_CAMERA_AUTOSAVE_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-autosave-")


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Offscreen Qt uses its BASIC font database, which on Windows is empty unless
# pointed at the system fonts: every label is then a row of placeholder boxes
# of the wrong width (docs/DEVELOPER_NOTES.md section 9, the renderer). The
# layout tests MEASURE widths (the AutoFocus tab must fit the lab screen), so
# they need the real fonts. Must be set before the first QApplication.
import os  # noqa: E402

_FONTS = r"C:\Windows\Fonts"
if os.path.isdir(_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONTS)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _own_pattern_autosave_folder(tmp_path_factory, monkeypatch):
    """Every test gets an EMPTY autosave folder: a pattern autosaved by one
    test (it carries an objective, a pixel size, a scan array) must not be
    restored into the next test's camera."""
    monkeypatch.setenv("AALTOFLOW_CAMERA_AUTOSAVE_DIR",
                       str(tmp_path_factory.mktemp("autosave")))
