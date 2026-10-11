"""The Measurement tab's compact layout (2026-10-12).

Lukas, with a screenshot at 2000x1280: "way too cramped to see any data" --
the RESULT plot had ~90 px. The run pane was rebuilt: one run bar, Run info /
Options / Files as pop-up panels, the plot in a splitter taking at least half
the tab, the queue card and the log side by side under it. These tests pin
what that promised: the plot's share at two window sizes, the buttons that
show state without being opened, the pop-ups, the one-line server state.
Offscreen, no hardware.
"""

from __future__ import annotations

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

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from test_scan_server_suite import fast_polls, pump, qapp, rig  # noqa: E402,F401  (fixtures)


@pytest.fixture
def suite(qapp, tmp_path, monkeypatch):
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    from apps.suite import Suite
    win = Suite(root=tmp_path, out_dir=tmp_path / "data", follow=False)
    yield win
    win.close()


def _show_measurement(app, win, w, h):
    win.resize(w, h)
    win.show()
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    win.tabs.setCurrentIndex(names.index("Measurement"))
    for _ in range(30):
        app.processEvents()
    return win.tabs.currentWidget()


@pytest.mark.parametrize("size, share", [((2000, 1280), 0.45), ((1280, 800), 0.35)])
def test_the_plot_gets_most_of_the_tab(qapp, suite, size, share):
    tab = _show_measurement(qapp, suite, *size)
    b = suite.builder
    # the window really is that size: no other tab holds it wider (the Data
    # and Catalogue tabs scroll instead -- they used to force ~1500 px)
    assert suite.width() == size[0]
    got = b.view.height() / tab.height()
    assert got >= share, f"plot {b.view.height()} of {tab.height()} px = {got:.2f}"
    # nothing cut off: every button of the run bar is on screen, inside the pane
    for btn in (b.run_btn, b.pause_btn, b.abort_btn, b.run_info_btn, b.options_btn,
                b.files_btn):
        r = QtCore.QRect(btn.mapTo(b.right_pane, QtCore.QPoint(0, 0)), btn.size())
        assert b.right_pane.rect().contains(r), btn.text()
    # the log moved under the plot, next to the queue card
    assert b.right_pane.isAncestorOf(suite.logbox)


def test_the_log_goes_back_under_the_tabs_elsewhere(qapp, suite):
    _show_measurement(qapp, suite, 1500, 950)
    assert suite.builder.right_pane.isAncestorOf(suite.logbox)
    names = [suite.tabs.tabText(i) for i in range(suite.tabs.count())]
    suite.tabs.setCurrentIndex(names.index("Scan"))
    qapp.processEvents()
    assert not suite.builder.right_pane.isAncestorOf(suite.logbox)
    assert suite.logbox.isVisibleTo(suite)


def test_the_run_info_button_says_empty_or_how_many(suite):
    b = suite.builder
    b.run_info.set_values({})
    assert b.run_info_btn.text() == "Run info (empty)"
    assert "color" in b.run_info_btn.styleSheet()          # the warning colour
    b.run_info.set_values({"sample": "YIG 7", "operator": "LF"})
    assert b.run_info_btn.text() == "Run info (2)"
    assert b.run_info_btn.styleSheet() == ""


def test_the_files_button_turns_red_on_a_bad_save_target(suite, tmp_path):
    b = suite.builder
    b.autosave_dir = tmp_path / "data"
    assert b._refresh_save_target()
    assert b.files_btn.text() == "Files" and b.save_target_ok()
    b.autosave_dir = None
    assert not b._refresh_save_target()
    assert b.files_btn.text() == "Files (!)" and not b.save_target_ok()
    assert "background" in b.files_btn.styleSheet()
    assert "will not be saved" in b.save_lbl.text()
    assert "will not be saved" in b.files_btn.toolTip()     # readable without opening


def test_the_popups_open_close_and_keep_their_values(qapp, suite):
    _show_measurement(qapp, suite, 1500, 950)
    b = suite.builder
    panel = b.run_info_panel
    assert panel.isHidden()
    b.run_info_btn.click()
    qapp.processEvents()
    assert panel.isVisible() and b.run_info.isVisible()
    # the SAME card as ever lives in it
    assert panel.isAncestorOf(b.run_info)
    b.run_info.edits["sample"].setText("permalloy disc")
    b.run_info.edits["sample"].editingFinished.emit()
    panel.move(panel.pos() + QtCore.QPoint(37, 21))
    where = panel.pos()
    # Esc closes it
    QtWidgets.QApplication.sendEvent(
        panel, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape,
                               QtCore.Qt.NoModifier))
    qapp.processEvents()
    assert panel.isHidden()
    assert b.run_info.values()["sample"] == "permalloy disc"
    assert b.run_info_btn.text() == "Run info (1)"
    b.run_info_btn.click()                       # reopens where it was left
    qapp.processEvents()
    assert panel.isVisible() and panel.pos() == where
    assert b.run_info.edits["sample"].text() == "permalloy disc"
    b.run_info_btn.click()                       # the button closes it too
    qapp.processEvents()
    assert panel.isHidden()
    # Options: per-point and diagonal, the same widgets the summary reads
    b.options_btn.click()
    qapp.processEvents()
    assert b.options_panel.isAncestorOf(b.per_pt) and b.per_pt.isVisible()
    b.per_pt.setValue(0.25)
    b.options_panel.close()
    assert b.per_pt.value() == 0.25
    # Files: the load / save / recall / save-data buttons and the save line
    for w in (b.load_btn, b.save_btn, b.recall_btn, b.save_data_btn, b.save_lbl):
        assert b.files_panel.isAncestorOf(w)


def test_the_summary_is_one_line_with_warnings_coloured(suite):
    b = suite.builder
    b.detail.setText("dims: a, b   ·   WARNING: the autofocus runs before the scout")
    assert C_accent() in b.detail.styleSheet()
    b.detail.setText("• 'x' is outside its limits\n• WARNING: y")
    assert "• 'x' is outside" in b.detail.text()          # the whole text is kept
    assert "\n" in b.detail.toolTip()
    assert b.detail.sizeHint().height() < 2 * b.detail.fontMetrics().height()


def C_accent():
    from apps.theme import C
    return C["accent"]


def test_the_server_line_shows_only_while_watching(qapp, rig):
    srv, win, c = rig()
    pump(qapp, lambda: "watching scan server" in win.server_lbl.text())
    assert not win.server_strip.isHidden()
    # ONE thin line, not a banner
    assert win.server_strip.sizeHint().height() <= 44
    assert win.builder.right_pane.isAncestorOf(win.server_strip)
    win.stop_watching()
    assert win.server_strip.isHidden()


def test_a_plain_suite_has_no_server_line(suite):
    assert suite.server_strip.isHidden()
    assert suite.builder.server_box.isHidden()


def test_the_standalone_builder_wraps_its_run_bar(qapp):
    from apps.scan_builder import ScanBuilder
    sb = ScanBuilder()
    sb.resize(1280, 820)
    sb.show()
    for _ in range(20):
        qapp.processEvents()
    try:
        bar = sb.run_bar
        for btn in (sb.run_btn, sb.abort_btn, sb.files_btn):
            r = QtCore.QRect(btn.mapTo(bar, QtCore.QPoint(0, 0)), btn.size())
            assert bar.rect().contains(r), btn.text()
        # no log there: the area under the plot only appears for a server
        assert sb.bottom_split.isHidden()
        sb.files_btn.click()
        qapp.processEvents()
        assert sb.files_panel.isVisible()
        sb.files_panel.close()
    finally:
        sb.close()
