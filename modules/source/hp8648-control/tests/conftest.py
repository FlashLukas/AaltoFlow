"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `hp8648` straight from src/, which is handy while iterating.
"""

import os
import sys
import tempfile

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="aaltoflow-nosec-")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Hardware claims (hp8648.hwlock) go to a temp folder in EVERY test, so a
    test that opens the real backend against a fake pyvisa never collides with
    -- or blocks -- an hp8648 service running on this PC."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "hwlocks"))
