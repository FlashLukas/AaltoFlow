"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `dsphase` straight from src/, which is handy while iterating.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test gets its own hardware-lock folder (hwlock.py). Without this
    the real backend tests would claim "COM5" in the REAL lock folder of this
    PC, and could collide with a service Lukas has running."""
    d = tmp_path / "locks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(d))
    return d
