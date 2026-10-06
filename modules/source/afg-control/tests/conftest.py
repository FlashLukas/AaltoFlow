"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `afg` straight from src/, which is handy while iterating.
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
    r"""Every test gets its own hardware-lock folder (hwlock.py).

    Without it, a test that opens the real backend on "COM7" would write a
    lock file into the user's real %LOCALAPPDATA%\AaltoFlow\locks, and two
    tests in one run would refuse each other (one process may not claim an
    address twice). Offline and isolated, as every test here must be.
    """
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
