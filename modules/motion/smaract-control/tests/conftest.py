"""Make the src/ layout importable during tests without installing."""

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
