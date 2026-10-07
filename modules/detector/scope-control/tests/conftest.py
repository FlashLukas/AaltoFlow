"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `scope` straight from src/, which is handy while iterating.
"""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")
# The GUI's per-PC preferences (tab, splitter widths, XY/YX) go to a throw-away
# file, never into the user's registry (apps/gui.py `_gui_settings`).
_os.environ["AALTOFLOW_GUI_SETTINGS"] = _os.path.join(
    _tempfile.mkdtemp(prefix="aaltoflow-gui-"), "gui.ini")


import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test claims hardware addresses in its OWN folder, never in the
    PC's real lock folder (LOCALAPPDATA/AaltoFlow/locks): a test must not collide with a
    service Lukas has running (or with the previous test)."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
