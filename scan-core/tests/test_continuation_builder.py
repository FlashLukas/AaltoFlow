"""Files > Continue... in the measurement suite (2026-10-14): the dialog picks
the point, the run pane runs the continued scan into a NEW file."""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                  # noqa: E402

from apps.scan_builder import ScanBuilder                      # noqa: E402
from scan_core import build_sim_registry, run                  # noqa: E402
from scan_core import continuation as CT                       # noqa: E402
from scan_core.autosave import write_dataset                   # noqa: E402
from scan_core.recipe import Recipe                            # noqa: E402


@pytest.fixture
def builder(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    win.ask_before_unsaved = False
    win.autosave_dir = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    yield win
    win.close()


def _early(tmp_path, n=4):
    recipe = Recipe(name="cont_gui", detectors=["lockin_r"], axes=[
        {"type": "linear", "param": "field", "start": -20, "stop": 20, "num": 9}])
    done = {"n": 0}
    ds = run(recipe, build_sim_registry(), should_abort=lambda: done["n"] >= n,
             on_progress=lambda d, t, eta: done.__setitem__("n", d))
    path = tmp_path / "early.nc"
    write_dataset(ds, path)
    return path, ds


def _wait(builder, timeout=20.0):
    t0 = time.monotonic()
    while builder.worker is not None and time.monotonic() - t0 < timeout:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    for _ in range(20):
        QtWidgets.QApplication.processEvents()
    assert builder.worker is None, "the continued scan did not finish"


def test_the_dialog_offers_the_first_missing_point(builder, tmp_path):
    path, _ = _early(tmp_path)
    prog = CT.read_progress(path)
    from apps.continue_dialog import ContinueDialog
    dlg = ContinueDialog(prog, [])
    assert dlg.point.value() == 5 and dlg.start == 4
    assert "field = 0 mT (5/9)" in dlg.where.text()
    assert dlg.go.isEnabled()
    dlg.point.setValue(2)
    assert "1 measured points kept" in dlg.counts.text()
    assert not ContinueDialog(prog, ["no"]).go.isEnabled()


def test_continue_runs_into_a_new_file_and_keeps_the_earlier_points(builder, tmp_path):
    path, early = _early(tmp_path)
    builder.open_continue(path, start=2, ask=False)
    assert builder.worker is not None
    _wait(builder)
    files = sorted((tmp_path / "data").rglob("*.nc"))
    assert len(files) == 1 and files[0] != path
    import xarray as xr
    with xr.open_dataset(files[0], engine="h5netcdf") as ds:
        ds = ds.load()
    assert ds.attrs["continued_from"] == "early.nc"
    assert ds.attrs["continued_at_point"] == 3
    assert np.array_equal(ds["lockin_r"].values[:2], early["lockin_r"].values[:2])
    assert np.isfinite(ds["lockin_r"].values).all()
    # the definition is in the Scan tab too
    assert builder.build_recipe().axes[0]["param"] == "field"
    assert path.exists()                                   # the earlier file untouched


def test_a_file_that_cannot_be_continued_does_not_start(builder, tmp_path):
    path, _ = _early(tmp_path)
    # a grid the instruments cannot reproduce: the field limits shrink
    builder.registry.get("field").limits = (-5.0, 5.0)
    dlg = builder.open_continue(path, ask=False)
    assert dlg is not None and not dlg.go.isEnabled()
    assert builder.worker is None
