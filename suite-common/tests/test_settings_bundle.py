"""Settings export / import against throwaway suite folders.

Two roots per test where it matters: "old" (the PC the bundle comes from) and
"new" (the PC it goes to), so the round trip is a real move between installs.
"""

import json
import zipfile
from pathlib import Path

import pytest

from suite_common import settings_bundle as B

from test_modules import make_module


def _suite(root: Path) -> Path:
    """A suite with two modules, lab data in each, and the named files."""
    kim = make_module(root, "kim-control", "kim", 5567)
    (kim / "kim.ini").write_text("[limits]\nmax = 1\n", encoding="utf-8")
    (kim / "px_calibration.json").write_text('{"x": 21.1}', encoding="utf-8")
    (kim / "pyproject.toml").write_text("code, not a setting", encoding="utf-8")
    clmag = make_module(root, "clMag-control", "clMag", 5555)
    (clmag / "Calibrations" / "old").mkdir(parents=True)
    (clmag / "Calibrations" / "cal_2026.json").write_text("{}", encoding="utf-8")
    (clmag / "Calibrations" / "old" / "cal_2025.json").write_text("{}", encoding="utf-8")
    (clmag / "Calibrations" / "__pycache__").mkdir()
    (clmag / "Calibrations" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (root / "suite_local.json").write_text('{"modules": {"kim": {"real": true}}}',
                                           encoding="utf-8")
    (root / "mission-control").mkdir()
    (root / "mission-control" / "profiles.json").write_text("[]", encoding="utf-8")
    (root / "scan-core").mkdir()
    (root / "scan-core" / "suite_layouts.json").write_text("{}", encoding="utf-8")
    (root / "scan-core" / "notes.txt").write_text("not a setting", encoding="utf-8")
    return root


EXPECTED = [
    "clMag-control/Calibrations/cal_2026.json",
    "clMag-control/Calibrations/old/cal_2025.json",
    "kim-control/kim.ini",
    "kim-control/px_calibration.json",
    "mission-control/profiles.json",
    "scan-core/suite_layouts.json",
    "suite_local.json",
]


@pytest.fixture
def old(tmp_path):
    return _suite(tmp_path / "old")


def test_collect_follows_the_rule_not_a_list(old):
    assert B.collect(old) == EXPECTED
    # a module added later is covered without editing anything
    new = make_module(old, "vna-control", "vna", 5573)
    (new / "vna.ini").write_text("", encoding="utf-8")
    assert "vna-control/vna.ini" in B.collect(old)


def test_manifest_lists_the_files_and_nothing_about_the_pc(old, tmp_path):
    dest = tmp_path / "out" / "bundle.zip"
    saved = B.export_bundle(dest, old)
    assert saved == EXPECTED
    with zipfile.ZipFile(dest) as zf:
        man = json.loads(zf.read(B.MANIFEST_NAME))
        assert set(zf.namelist()) == set(EXPECTED) | {B.MANIFEST_NAME}
    assert man["format"] == B.BUNDLE_FORMAT and man["product"] == "AaltoFlow"
    assert [f["path"] for f in man["files"]] == EXPECTED
    assert all(len(f["sha256"]) == 64 for f in man["files"])
    assert set(man) == {"format", "product", "created", "note", "files"}


def test_round_trip_to_a_fresh_install(old, tmp_path):
    bundle = tmp_path / "b.zip"
    B.export_bundle(bundle, old)
    new = tmp_path / "new"
    make_module(new, "kim-control", "kim", 5567)
    make_module(new, "clMag-control", "clMag", 5555)
    (new / "mission-control").mkdir()
    (new / "scan-core").mkdir()
    plan = B.read_bundle(bundle, new)
    assert [e.path for e in plan.new] == EXPECTED
    assert not plan.overwrite and not plan.skipped
    assert plan.touches_local_settings
    assert "suite_local.json" in B.summary(plan)
    backup = B.apply_import(plan, new)
    assert backup is None                         # nothing was replaced
    for rel in EXPECTED:
        assert (new / rel).read_bytes() == (old / rel).read_bytes(), rel
    # importing it again changes nothing
    again = B.read_bundle(bundle, new)
    assert [e.path for e in again.unchanged] == EXPECTED and not again.to_write


def test_overwrite_keeps_a_backup_that_undoes_the_import(old, tmp_path):
    bundle = tmp_path / "b.zip"
    B.export_bundle(bundle, old)
    ini = old / "kim-control" / "kim.ini"
    ini.write_text("[limits]\nmax = 999\n", encoding="utf-8")   # tuned since
    plan = B.read_bundle(bundle, old)
    assert [e.path for e in plan.overwrite] == ["kim-control/kim.ini"]
    backup = B.apply_import(plan, old)
    assert backup.parent == old / ".suite_cache"
    assert backup.name.startswith("settings-backup-")
    assert "max = 1\n" in ini.read_text(encoding="utf-8")
    # the backup is itself a bundle: importing it puts the tuned file back
    undo = B.read_bundle(backup, old)
    assert [e.path for e in undo.overwrite] == ["kim-control/kim.ini"]
    B.apply_import(undo, old)
    assert "max = 999" in ini.read_text(encoding="utf-8")


def _handmade(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(B.MANIFEST_NAME, json.dumps({"format": B.BUNDLE_FORMAT}))
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def test_zip_slip_code_and_unknown_modules_are_skipped(old, tmp_path):
    bundle = _handmade(tmp_path / "evil.zip", {
        "../escape.ini": "x",
        "kim-control/../../escape2.ini": "x",
        "C:/Windows/evil.ini": "x",
        "/abs.ini": "x",
        "kim-control\\..\\..\\bs.ini": "x",
        "kim-control/src/kim/backends/sim.py": "import os",   # code, not a setting
        "kim-control/pyproject.toml": "x",
        "hf2-control/hf2.ini": "x",                            # not installed here
        "kim-control/kim.ini": "[ok]\n",
    })
    plan = B.read_bundle(bundle, old)
    skipped = {e.path: e.reason for e in plan.skipped}
    assert len(skipped) == 8
    assert sum("unsafe" in r for r in skipped.values()) == 5
    assert "not a settings file" in skipped["kim-control/src/kim/backends/sim.py"]
    assert "not a settings file" in skipped["kim-control/pyproject.toml"]
    assert "not installed" in skipped["hf2-control/hf2.ini"]
    assert [e.path for e in plan.to_write] == ["kim-control/kim.ini"]
    B.apply_import(plan, old)
    assert not (tmp_path / "escape.ini").exists()
    assert not (old / "hf2-control").exists()
    assert (old / "kim-control" / "kim.ini").read_text(encoding="utf-8") == "[ok]\n"


def test_named_file_of_a_missing_suite_folder_is_skipped(old, tmp_path):
    bundle = tmp_path / "b.zip"
    B.export_bundle(bundle, old)
    new = tmp_path / "new"
    make_module(new, "kim-control", "kim", 5567)          # no scan-core, no clMag
    plan = B.read_bundle(bundle, new)
    skipped = {e.path for e in plan.skipped}
    assert "scan-core/suite_layouts.json" in skipped
    assert "clMag-control/Calibrations/cal_2026.json" in skipped
    assert {e.path for e in plan.new} == {"kim-control/kim.ini",
                                          "kim-control/px_calibration.json",
                                          "suite_local.json"}


def test_not_a_bundle_is_refused(tmp_path):
    plain = tmp_path / "x.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("kim-control/kim.ini", "x")
    with pytest.raises(ValueError, match="not an AaltoFlow settings bundle"):
        B.read_bundle(plain, tmp_path)
    (tmp_path / "y.zip").write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="not a zip"):
        B.read_bundle(tmp_path / "y.zip", tmp_path)
    newer = tmp_path / "z.zip"
    with zipfile.ZipFile(newer, "w") as zf:
        zf.writestr(B.MANIFEST_NAME, json.dumps({"format": "aaltoflow-settings/9"}))
    with pytest.raises(ValueError, match="newer version"):
        B.read_bundle(newer, tmp_path)


def test_summary_is_ascii(old, tmp_path):
    bundle = tmp_path / "b.zip"
    B.export_bundle(bundle, old)
    B.summary(B.read_bundle(bundle, old)).encode("ascii")
