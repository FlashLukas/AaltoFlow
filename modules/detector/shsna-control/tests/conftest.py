"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `shsna` straight from src/, which is handy while iterating.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test claims analysers in its OWN folder (shsna.hwlock honours
    AALTOFLOW_LOCK_DIR), never in the PC's real lock folder (%LOCALAPPDATA%/AaltoFlow/locks)
    -- so the tests cannot collide with a running service, or with each other."""
    d = tmp_path / "locks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(d))
    return d
