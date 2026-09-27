"""tools/migrate_layout.py -- tidying a checkout after the move to modules/.

The situation on a rig after `git pull`: the module's tracked files are in
modules/<category>/<key>-control, and the old <key>-control folder still holds
the files git ignores (tuned .ini, calibrations, notes, a stale .venv).
"""

import sys
from pathlib import Path

import pytest

from test_modules import make_module

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import migrate_layout as ML  # noqa: E402


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "suite"
    new = make_module(root, "modules/motion/kim-control", "kim", 5567)
    (new / "kim.ini").write_text("factory")                  # a different file, same name
    (new / "README.md").write_text("same text")
    old = root / "kim-control"                                 # what the pull left behind
    (old / "Calibrations").mkdir(parents=True)
    (old / "Calibrations" / "2026-09-01.json").write_text("rig calibration")
    (old / "px_calibration.json").write_text('{"x": 21.1}')
    (old / "kim.ini").write_text("tuned on the rig")
    (old / "README.md").write_text("same text")
    (old / "CLAUDE.local.md").write_text("notes")
    (old / "out").mkdir()
    (old / "out" / "scan.nc").write_bytes(b"data")
    (old / ".venv" / "Lib").mkdir(parents=True)
    (old / ".venv" / "Lib" / "x.pth").write_text("old src")
    (old / "src" / "kim.egg-info").mkdir(parents=True)
    (old / "src" / "kim.egg-info" / "PKG-INFO").write_text("x")
    (old / "src" / "kim" / "__pycache__").mkdir(parents=True)
    (old / "src" / "kim" / "__pycache__" / "a.pyc").write_bytes(b"x")
    return root, old, new


def test_dry_run_changes_nothing(rig):
    root, old, new = rig
    rep = ML.migrate(root, apply=False)
    assert rep.count("move") == 4 and rep.count("keep-both") == 1 and rep.count("same") == 1
    assert rep.count("delete") == 3                           # .venv, egg-info, __pycache__
    assert (old / "kim.ini").read_text() == "tuned on the rig"
    assert (old / ".venv").exists() and not (new / "px_calibration.json").exists()


def test_apply_moves_lab_data_and_never_overwrites(rig):
    root, old, new = rig
    rep = ML.migrate(root, apply=True)
    assert rep.count("error") == 0
    assert not old.exists()                                   # empty, so removed
    assert (new / "Calibrations" / "2026-09-01.json").read_text() == "rig calibration"
    assert (new / "px_calibration.json").read_text() == '{"x": 21.1}'
    assert (new / "CLAUDE.local.md").read_text() == "notes"
    assert (new / "out" / "scan.nc").read_bytes() == b"data"
    # a different file of the same name: both kept, the newer one untouched
    assert (new / "kim.ini").read_text() == "factory"
    assert (new / "kim.ini.old-layout").read_text() == "tuned on the rig"
    assert not (new / "README.md.old-layout").exists()        # identical: just dropped
    assert not (new / ".venv").exists()
    assert rep.touched == ["modules/motion/kim-control"]
    # and a second run has nothing left to do
    assert ML.migrate(root, apply=True).steps == []


def test_a_whole_second_copy_is_left_for_a_person(rig):
    """An old folder with its own module.toml is not leftovers: it is a second
    copy of the module (maybe edited). Never merged automatically."""
    root, old, new = rig
    (old / "module.toml").write_text((new / "module.toml").read_text())
    (old / "scripts").mkdir()
    (old / "scripts" / "run_service.py").write_text("")
    (old / "scripts" / "run_gui.py").write_text("")
    rep = ML.migrate(root, apply=True)
    assert [s.what for s in rep.steps] == ["skip"]
    assert (old / "kim.ini").read_text() == "tuned on the rig"


def test_the_real_checkout_has_nothing_left_to_migrate():
    """This repository itself: every module is in modules/, no old folder."""
    rep = ML.migrate(ML.ROOT, apply=False)
    assert rep.steps == []
