"""Make the src-layout package importable in tests without installing it."""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    # Real-backend tests claim a GPIB address through hwlock. Keep those lock
    # files in a per-test temp folder, so a test can never collide with (or be
    # blocked by) a real sr830 service running on this PC.
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
