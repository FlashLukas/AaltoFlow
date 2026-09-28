"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `signalhound` straight from src/, which is handy while iterating.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Hardware claims (hwlock) go to a per-test folder, never to the real
    %LOCALAPPDATA%/AaltoFlow/locks -- so a test cannot see (or block) a
    service Lukas has running, and tests cannot block each other."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
