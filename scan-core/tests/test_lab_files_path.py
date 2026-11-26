"""apps/lab_files.cache_path: a file path from a watched scan server must stay
inside this PC's cache folder (security review 2026-11-26). Before, a drive
path ("C:\...") replaced the cache folder and a server could write anywhere."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from apps.lab_files import cache_path  # noqa: E402

ROOT = (Path(tempfile.gettempdir()) / "aaltoflow-lab-data").resolve()


def test_a_normal_measurement_lands_in_the_cache():
    p = cache_path("lab-pc", "2026-11-26/101500_map.nc")
    assert p == ROOT / "lab-pc" / "2026-11-26" / "101500_map.nc"


@pytest.mark.parametrize("rel", [
    r"C:\Users\Public\evil.nc",
    "C:/Users/Public/x.nc",
    r"\\host\share\x.nc",          # a UNC path
    r"\rooted\x.nc",
    "/etc/x.nc",
    "../../x.nc",
    "2026/../../x.nc",
    "2026/run.py",                 # not a measurement
    "",
])
def test_paths_that_leave_the_cache_or_are_not_measurements_are_refused(rel):
    with pytest.raises(ValueError):
        cache_path("lab-pc", rel)


@pytest.mark.parametrize("pc", ["..", ".", "", "../x"])
def test_a_pc_name_cannot_leave_the_cache(pc):
    p = cache_path(pc, "a.nc")
    p.relative_to(ROOT)            # raises if outside
    assert p.parent != ROOT.parent
