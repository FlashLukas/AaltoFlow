"""Make the src/ layout importable during tests without installing."""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test gets its OWN hardware-lock folder (hwlock.py), so a test that
    opens the real backend against a fake DLL never touches the locks of a
    service Lukas has running on this PC, and tests cannot see each other's."""
    d = tmp_path / "hwlocks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(d))
    return d
