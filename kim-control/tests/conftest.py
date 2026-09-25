"""Make the src/ layout importable during tests without installing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def px_file_per_test(monkeypatch, tmp_path):
    """Every test gets its OWN px/step calibration file, in a temp folder.

    Two reasons. The brain loads `px_calibration.json` from the project folder
    at start-up, and since 2026-09-16 that file DECIDES the um-per-step of X
    and Y -- so without this the numbers in these tests would depend on whether
    the lab left a calibration in the working copy (green here, red on a fresh
    clone). And a test that RUNS a calibration must not write into the project
    folder, nor hand its result to the next test.
    """
    monkeypatch.setattr("kim.kim.Kim.px_file",
                        lambda self, _p=tmp_path / "px_calibration.json": _p)
