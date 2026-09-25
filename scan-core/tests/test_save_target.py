"""Naming the measurement, and proving it can be saved BEFORE the scan runs.

From the rig, 2026-09-16: "i would like to have the name of the file already
here and checked that it can be saved". A scan is minutes to hours; discovering
at the end that the folder is read-only, or the share is gone, means the
measurement exists only in memory.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                  # noqa: E402

from apps.scan_builder import ScanBuilder                      # noqa: E402
from scan_core import build_sim_registry                       # noqa: E402
from scan_core.recipe import Recipe                            # noqa: E402


@pytest.fixture
def builder():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    win.ask_before_unsaved = False          # never block a test on a dialog
    yield win
    win.close()


def test_the_name_you_type_is_the_name_on_the_file(builder, tmp_path):
    builder.autosave_dir = tmp_path
    builder.name_edit.setText("kerr map 63x")
    path = builder.autosave_path(builder.build_recipe())
    # spaces are not a file name's friend, but the name is otherwise yours
    assert path.name.endswith("_kerr_map_63x.nc")
    assert path.parent.parent == tmp_path        # <data dir>/<date>/<file>
    assert "kerr_map_63x" in builder.preview_path()


def test_the_name_travels_in_the_definition_and_comes_back(builder):
    """So a measurement file reopened next week still knows what it was."""
    builder.name_edit.setText("fmr sweep")
    assert builder.build_recipe().name == "fmr sweep"
    builder.load_recipe(Recipe(name="older run", axes=[], detectors=[]))
    assert builder.name_edit.text() == "older run"


def test_an_empty_name_still_produces_a_file(builder, tmp_path):
    builder.autosave_dir = tmp_path
    builder.name_edit.setText("   ")
    assert builder.autosave_path(builder.build_recipe()).name.endswith("_scan.nc")


def test_a_writable_folder_reports_where_the_data_will_go(builder, tmp_path):
    builder.autosave_dir = tmp_path
    ok, msg = builder.check_save_target()
    assert ok and str(tmp_path) in msg


def test_the_check_does_not_leave_a_folder_behind(builder, tmp_path):
    """It runs on every keystroke in the name box. A day with no measurement
    must not end up with an empty dated folder in the data directory."""
    builder.autosave_dir = tmp_path / "data"
    builder.name_edit.setText("probing")
    assert builder.check_save_target()[0] is True      # the parent is writable
    assert list(tmp_path.iterdir()) == [], "nothing may be created by looking"


def test_an_unwritable_folder_is_caught_before_the_scan(builder, tmp_path):
    """The whole point: report it now, not in an hour."""
    target = tmp_path / "read_only"
    target.mkdir()
    if os.name == "nt":
        # Windows ignores chmod on directories; deny writes through an ACL.
        import subprocess
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(target), "/deny", f"{user}:(W)"],
                       capture_output=True, check=False)
    else:
        target.chmod(0o500)
    try:
        builder.autosave_dir = target
        ok, msg = builder.check_save_target()
        if not ok:                       # skip where the OS let us write anyway
            assert "CANNOT SAVE" in msg
            assert builder._refresh_save_target() is False
        else:
            pytest.skip("this filesystem ignored the deny rule")
    finally:
        if os.name == "nt":
            import subprocess
            subprocess.run(["icacls", str(target), "/remove:d",
                            os.environ.get("USERNAME", "")],
                           capture_output=True, check=False)
        else:
            target.chmod(0o700)


def test_no_data_directory_is_itself_a_warning(builder):
    builder.autosave_dir = None
    ok, msg = builder.check_save_target()
    assert not ok and "not be saved" in msg


def test_a_scan_that_cannot_be_saved_asks_first(builder, tmp_path, monkeypatch):
    """Run must not quietly start a two-hour measurement with nowhere to put it.

    With the dialog suppressed (as here) it goes ahead, which is what the
    'Run without saving' button does.
    """
    asked = []
    monkeypatch.setattr(ScanBuilder, "_confirm_unsaved",
                        lambda self, why: asked.append(why) or True)
    builder.autosave_dir = None
    builder.add_axis("field")
    builder.rows[0].num.setValue(3)
    builder.per_pt.setValue(0.0)
    builder.run_scan()
    try:
        assert asked, "Run started without mentioning that nothing would be saved"
        assert "not be saved" in asked[0]
    finally:
        builder._abort()
        if builder.worker is not None:
            builder.worker.wait(3000)
