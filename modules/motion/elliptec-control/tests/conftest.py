"""Make the src/ layout importable during tests without installing."""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _private_lock_dir(tmp_path, monkeypatch):
    """Every test claims hardware addresses in its OWN temp folder.

    The real backend claims its COM port in open() (hwlock.py). Without this,
    tests would write lock files into the user's real %LOCALAPPDATA% folder --
    and a test run could refuse, or be refused by, a service Lukas has running
    on the same port.
    """
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
