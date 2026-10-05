"""The suite's Catalogue tab (apps/catalogue_view.py), offscreen.

The index itself is tested in test_catalogue.py; here: the rescan runs in a
worker thread and fills the table, the search fields narrow it, a bad `where`
says so instead of crashing, and a double-click hands the file to the Data
tab's viewer.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6 import QtCore, QtWidgets                                  # noqa: E402

from apps.catalogue_view import CatalogueWidget                         # noqa: E402
from test_catalogue import folder, _write                              # noqa: E402,F401


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _scanned(widget):
    widget.rescan()
    widget.wait_scan()
    deadline = time.monotonic() + 10
    while widget.table.topLevelItemCount() == 0 and time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
    return widget


def _names(widget):
    return sorted(widget.table.topLevelItem(i).text(1)
                  for i in range(widget.table.topLevelItemCount()))


def test_rescan_fills_the_table_in_a_worker_thread(qapp, folder):
    opened = []
    w = _scanned(CatalogueWidget(folder, open_file=opened.append))
    assert w.table.topLevelItemCount() == 5
    assert "5 run(s)" in w.status.text() and "1 unreadable" in w.status.text()
    assert w.rescan_btn.isEnabled() and w.progress.isHidden()
    assert (folder / "catalogue.sqlite").exists()


def test_search_fields_narrow_the_table(qapp, folder):
    w = _scanned(CatalogueWidget(folder))
    w.text_edit.setText("b7"); w.refresh()
    assert _names(w) == ["cold sweep", "fmr map"]
    w.operator_edit.setText("anna"); w.refresh()
    assert _names(w) == ["cold sweep"]
    w.clear_filters()
    assert w.table.topLevelItemCount() == 5
    w.where_edit.setText("ppms.temperature between 4 and 6"); w.refresh()
    assert _names(w) == ["fmr map"]
    w.clear_filters()
    w.tags_edit.setText("moke"); w.refresh()
    assert _names(w) == ["other sample"]
    w.clear_filters()
    w.from_edit.setText("2026-10-02"); w.refresh()
    assert "fmr map" not in _names(w) and "cold sweep" in _names(w)


def test_typing_searches_after_a_pause(qapp, folder):
    w = _scanned(CatalogueWidget(folder))
    w.sample_edit.setText("Y12")                 # no refresh() call: the timer does it
    deadline = time.monotonic() + 3
    while w.table.topLevelItemCount() != 1 and time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.02)
    assert _names(w) == ["other sample"]


def test_a_bad_where_is_reported_not_raised(qapp, folder):
    w = _scanned(CatalogueWidget(folder))
    w.where_edit.setText("x == 1; DROP TABLE files"); w.refresh()
    assert w.status.text().startswith("where:")
    w.where_edit.clear(); w.from_edit.setText("yesterday"); w.refresh()
    assert "YYYY-MM-DD" in w.status.text()


def test_double_click_opens_the_file_and_selection_shows_details(qapp, folder):
    opened = []
    w = _scanned(CatalogueWidget(folder, open_file=opened.append))
    w.text_edit.setText("disc"); w.refresh()
    item = w.table.topLevelItem(0)
    w.table.setCurrentItem(item)
    assert "first look" in w.details.text() and "ppms" in w.details.text()
    w.table.itemActivated.emit(item, 0)
    assert opened == [str(folder / "2026-10-01" / "100000_fmr_map.nc")]
    # an unreadable file is not handed to the viewer
    w.text_edit.setText("broken"); w.refresh()
    w.table.itemActivated.emit(w.table.topLevelItem(0), 0)
    assert len(opened) == 1 and "cannot open" in w.status.text()


def test_the_suite_tab_opens_a_run_in_the_data_viewer(qapp, folder, tmp_path, monkeypatch):
    import apps.control_panel as cp
    import apps.suite as suite_mod
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    monkeypatch.setattr(suite_mod, "set_setting", lambda *a, **k: None)
    win = suite_mod.Suite(out_dir=folder)
    try:
        names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert "Catalogue" in names
        cat_w = win.catalogue
        assert cat_w.data_dir == folder
        _scanned(cat_w)
        cat_w.text_edit.setText("cold"); cat_w.refresh()
        assert cat_w.table.topLevelItemCount() == 1
        loaded = []
        monkeypatch.setattr(win.data_view, "load_file", lambda p: loaded.append(Path(p)))
        cat_w.table.itemActivated.emit(cat_w.table.topLevelItem(0), 0)
        assert loaded == [folder / "2026-10-02" / "110000_cold.nc"]
        assert win.tabs.tabText(win.tabs.currentIndex()) == "Data"
        # a new data folder reaches the catalogue too
        other = tmp_path / "elsewhere"; other.mkdir()
        win.set_out_dir(other)
        assert cat_w.data_dir == other and cat_w.table.topLevelItemCount() == 0
    finally:
        win.close()


def test_setup_sample_operator_are_pick_lists_of_the_values_present(tmp_path):
    """Lukas, 2026-10-05: the catalogue offers Setup, User and Sample as lists."""
    from scan_core import catalogue
    from scan_core.autosave import write_dataset
    from scan_core.engine import run
    from scan_core.recipe import Recipe
    from scan_core.registry import build_sim_registry
    reg = build_sim_registry()
    for k, (setup, sample, op) in enumerate([("TR-MOKE", "B7", "anna"),
                                             ("VNA-FMR", "Y12", "ben"),
                                             ("TR-MOKE", "B8", "anna")]):
        r = Recipe(name=f"s{k}", axes=[{"type": "linear", "param": "field",
                                        "start": 0, "stop": 1, "num": 2}],
                   detectors=["lockin_r"])
        ds = run(r, reg, attrs={"setup_name": setup, "sample": sample, "operator": op})
        write_dataset(ds, tmp_path / "2026-10-05" / "sub" / f"12000{k}_s{k}.nc")  # a subfolder
    catalogue.scan(tmp_path)
    assert catalogue.distinct(tmp_path, "setup") == ["TR-MOKE", "VNA-FMR"]
    assert len(catalogue.search(tmp_path, setup="tr-moke")) == 2

    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apps.catalogue_view import CatalogueWidget
    w = CatalogueWidget(data_dir=tmp_path)
    w.fill_picks()
    box = w._picks["setup"]
    assert [box.itemText(i) for i in range(box.count())] == ["", "TR-MOKE", "VNA-FMR"]
    assert [w._picks["operator"].itemText(i) for i in range(w._picks["operator"].count())] == ["", "anna", "ben"]
    box.setCurrentIndex(2)                       # choose VNA-FMR
    w.refresh()
    assert w.query()["setup"] == "VNA-FMR"
    assert w.table.topLevelItemCount() == 1
