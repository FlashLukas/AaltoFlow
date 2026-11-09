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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def ini_file_per_test(monkeypatch, tmp_path):
    """Every test gets its OWN kim001.ini path, in a temp folder.

    save_calibration / save_config default to the project's kim001.ini (lab
    data, loaded at the next start), and the lab's real one must never be
    written -- or read -- by a test.
    """
    monkeypatch.setattr("kim001.config.default_config_path",
                        lambda _p=tmp_path / "kim001.ini": _p, raising=False)
