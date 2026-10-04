"""Make the src-layout package importable in tests without installing it."""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")

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
