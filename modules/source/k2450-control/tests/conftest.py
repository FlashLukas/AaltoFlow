"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `k2450` straight from src/, which is handy while iterating.
"""

import os
import sys
import tempfile

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Set at import, before any test builds a
# socket.
os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="aaltoflow-nosec-")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test claims hardware addresses in its OWN temp folder, never in
    %LOCALAPPDATA%/AaltoFlow/locks -- so a test can neither be blocked by
    a real k2450 service running on this PC nor block one."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
