"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `smb` straight from src/, which is handy while iterating.
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
    """Every test gets its own hardware-lock folder (see smb/hwlock.py).

    Without this a test that opens the real backend against a fake pyvisa would
    write lock files into the SAME folder the lab's running services use -- and
    could be refused because a real service holds GPIB0::28 on this PC.
    """
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
