"""Make the src-layout package importable in tests without installing it."""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    # hwlock.py claims physical addresses with lock files in
    # %LOCALAPPDATA%\AaltoFlow\locks. Tests point it at a temp folder, so a
    # test run never collides with (or blocks) a real service on this PC.
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
