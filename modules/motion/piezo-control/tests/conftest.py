"""Make the src/ layout importable during tests without installing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Hardware claims (piezo.hwlock) go to a temp folder in EVERY test, so a
    test that opens the real d-Drive backend against a fake serial port never
    collides with -- or blocks -- a piezo service running on this PC."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "hwlocks"))
