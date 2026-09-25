"""Tests for the measurement suite: the four-tab operator application.

Offscreen, no hardware. The lab-registry tests run against the same
`FakeService` the rest of the suite's tests use, so a full Control tab -- real
manifests, actions, groups, moving limits -- can be exercised with nothing
plugged in.
"""

from __future__ import annotations

import copy
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

from PySide6 import QtCore, QtWidgets

from apps.control_panel import ControlPanel, ItemWidget, items_from_lab
from apps.suite import Suite
from scan_core.lab import build_lab_registry

from conftest import DEMO_MANIFEST


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def suite(qapp, tmp_path, monkeypatch):
    # Layouts must not be written into the checkout during a test run.
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    win = Suite()
    yield win
    win.close()


def _tick(panel, pid) -> bool:
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value():
        node = it.value()
        if node.data(0, QtCore.Qt.UserRole) == pid:
            node.setCheckState(0, QtCore.Qt.Checked)
            return True
        it += 1
    return False


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #

def test_the_tabs_exist_and_start_on_the_simulator(suite):
    assert [suite.tabs.tabText(i) for i in range(suite.tabs.count())] == \
        ["Control", "Navigator", "Scan", "Measurement", "Data", "Settings"]
    assert suite.lab is None, "should not reach for hardware on startup"
    assert suite.registry is not None
    assert "simulator" in suite.source_lbl.text()


def test_the_run_pane_moved_to_the_measurement_tab(suite):
    """The builder is not forked: its run pane is adopted by another tab.

    So the widgets the Measurement tab shows must be the very ones the builder
    drives -- not copies, which would leave Run wired to nothing.
    """
    names = [suite.tabs.tabText(i) for i in range(suite.tabs.count())]
    measurement = suite.tabs.widget(names.index("Measurement"))
    assert suite.builder.right_pane.parent() is not None
    assert suite.builder.right_pane.isAncestorOf(suite.builder.run_btn)
    assert measurement.isAncestorOf(suite.builder.right_pane)

    # ...and the Scan tab must NOT also contain it, or it would be in two places
    scan = suite.tabs.widget(names.index("Scan"))
    assert not scan.isAncestorOf(suite.builder.right_pane)


def test_the_data_tab_picks_up_the_run_that_just_finished(suite):
    """Opening Data after a scan should already show it -- that is what you
    went there for. It is a SEPARATE viewer from the run pane's, so scrubbing
    through a finished cube does not disturb the live one."""
    import numpy as np
    import xarray as xr

    assert suite.data_view.ds is None
    suite.builder.dataset = xr.Dataset(
        {"kerr": (("field", "freq"), np.zeros((3, 4)))},
        coords={"field": [0.0, 1.0, 2.0], "freq": [1.0, 2.0, 3.0, 4.0]})
    data_tab = [suite.tabs.tabText(i) for i in range(suite.tabs.count())].index("Data")
    suite.tabs.setCurrentIndex(data_tab)
    assert suite.data_view.ds is not None
    assert suite.data_view is not suite.builder.view


def test_the_data_directory_reaches_everything_that_uses_it(suite, tmp_path, monkeypatch):
    """Settings -> DATA is the ONE place the save location is chosen, so it has
    to reach the autosave, the Open dialog, and the next launch."""
    import apps.suite as suite_mod
    remembered = {}
    monkeypatch.setattr(suite_mod, "set_setting",
                        lambda name, value, root=None: remembered.update({name: value}))

    suite.set_out_dir(tmp_path)
    assert suite.builder.autosave_dir == tmp_path       # where a run is written
    assert suite.data_view.default_dir == tmp_path      # where Open... starts
    assert suite.out_edit.text() == str(tmp_path)
    assert remembered["data_dir"] == str(tmp_path)      # and it survives a restart


def test_a_typed_data_directory_applies_and_a_wrong_one_does_not(suite, tmp_path, monkeypatch):
    """A box you can type into must act on what you typed -- and must not
    silently create a folder out of a half-finished path."""
    import apps.suite as suite_mod
    monkeypatch.setattr(suite_mod, "set_setting", lambda *a, **k: None)

    good = tmp_path / "measurements"
    good.mkdir()
    suite.out_edit.setText(str(good))
    suite.out_edit.editingFinished.emit()
    assert suite.out_dir == good

    suite.out_edit.setText(str(tmp_path / "typo_half_typed"))
    suite.out_edit.editingFinished.emit()
    assert suite.out_dir == good, "an unknown folder must not become the target"
    assert not (tmp_path / "typo_half_typed").exists(), "and must not be created"
    assert suite.out_edit.text() == str(good), "the box goes back to the truth"


def test_scan_tab_still_defines_a_recipe(suite):
    b = suite.builder
    b.add_axis("field")
    b.rows[0].start.setValue(0.0)
    b.rows[0].stop.setValue(50.0)
    b.rows[0].num.setValue(5)
    recipe = b.build_recipe()
    assert recipe.axes[0]["param"] == "field"
    assert recipe.axes[0]["num"] == 5
    assert recipe.validate(b.registry) == []


# --------------------------------------------------------------------------- #
# The Control tab
# --------------------------------------------------------------------------- #

def test_control_tab_works_without_hardware(suite):
    """A simulated registry has no manifests; the tab must still be usable."""
    panel = suite.control
    assert panel.items, "no items from the simulated registry"
    assert all(i["module"] == "sim" for i in panel.items.values())
    # nothing is selected until you pick something
    assert panel.widgets == {}
    # isHidden() rather than isVisible(): nothing is "visible" while the
    # window has never been shown, so isVisible() would be False either way
    # and the assertion would prove nothing.
    assert not panel.empty.isHidden(), "the placeholder should be showing"

    assert _tick(panel, "field")
    assert "field" in panel.widgets
    assert panel.empty.isHidden(), "the placeholder should be gone"


def test_control_tab_builds_widgets_from_a_live_manifest(qapp, fake_service, tmp_path,
                                                         monkeypatch):
    """The whole point: controls, indicators and BUTTONS, from `describe`."""
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")

    svc = fake_service(15880, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port}, prefix=True)
    panel = ControlPanel()
    try:
        panel.set_source(registry=reg, lab=lab, prefix=True)
        kinds = {i["pid"]: i["kind"] for i in panel.items.values()}

        # actions ARE here, unlike in the scan registry -- that is the reason
        # the panel reads the manifest directly rather than through it
        assert kinds.get("fake.demag") == "action"
        assert reg.get("fake.demag") is None

        # and the enum control survives as a control, not demoted to read-only
        assert kinds.get("fake.mode") == "control"

        for pid in ("fake.field", "fake.enabled", "fake.mode", "fake.demag",
                    "fake.measured_field"):
            assert _tick(panel, pid), f"{pid} missing from the tree"

        assert isinstance(panel.widgets["fake.field"].editor,
                          QtWidgets.QDoubleSpinBox)
        assert isinstance(panel.widgets["fake.enabled"].editor,
                          QtWidgets.QCheckBox)
        assert isinstance(panel.widgets["fake.mode"].editor,
                          QtWidgets.QComboBox)
        assert isinstance(panel.widgets["fake.demag"].editor,
                          QtWidgets.QPushButton)

        # the spin box inherits the MODULE's limits
        spin = panel.widgets["fake.field"].editor
        assert (spin.minimum(), spin.maximum()) == (-95.0, 95.0)

        # every tree row says what KIND it is (marker + tooltip), so a button,
        # a readout and a settable value no longer look the same
        assert _tree_kind(panel, "fake.demag") == "action"
        assert _tree_kind(panel, "fake.measured_field") == "indicator"
        assert _tree_kind(panel, "fake.field") == "control"
        assert "Action" in _tree_node(panel, "fake.demag").toolTip(0)
        assert not _tree_node(panel, "fake.demag").icon(0).isNull()
    finally:
        panel.timer.stop()
        lab.close()


def test_control_tab_reclamps_when_the_limits_move(qapp, fake_service, tmp_path,
                                                   monkeypatch):
    """A panel offering travel the stage no longer has is how a move 'does
    nothing'. describe_rev is the signal; the spin box must follow it."""
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")

    svc = fake_service(15882, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port}, prefix=True)
    panel = ControlPanel()
    try:
        panel.set_source(registry=reg, lab=lab, prefix=True)
        assert _tick(panel, "fake.field")
        spin = panel.widgets["fake.field"].editor
        assert spin.maximum() == 95.0

        panel._refresh()                       # learns the current revision

        narrowed = copy.deepcopy(DEMO_MANIFEST)
        field = [p for p in narrowed["parameters"] if p["id"] == "field"][0]
        field["min"], field["max"] = -12.0, 12.0
        narrowed["revision"] = DEMO_MANIFEST["revision"] + 1
        svc.manifest = narrowed
        time.sleep(0.25)                       # let a status frame carry it

        panel._refresh()
        assert spin.maximum() == 12.0, "the panel kept the stale ceiling"
    finally:
        panel.timer.stop()
        lab.close()


def test_plot_traces_are_normalised_per_series(suite):
    """Mixed units on one axis make every small signal a flat line on the floor.

    Normalising per trace is what makes several channels readable at once; the
    absolute value goes in the legend and the readouts.
    """
    panel = suite.control
    assert _tick(panel, "field")
    w = panel.widgets["field"]
    w.item["plottable"] = True
    w.history = [1000.0, 1500.0, 2000.0]      # a big-numbered channel
    panel.curves.setdefault("field", panel.plot.plot([], []))

    panel._redraw_traces()
    _, y = panel.curves["field"].getData()
    assert y is not None
    assert min(y) == pytest.approx(0.0) and max(y) == pytest.approx(1.0)


def test_a_flat_trace_does_not_divide_by_zero(suite):
    panel = suite.control
    assert _tick(panel, "field")
    w = panel.widgets["field"]
    w.item["plottable"] = True
    w.history = [5.0, 5.0, 5.0]
    panel.curves.setdefault("field", panel.plot.plot([], []))

    panel._redraw_traces()
    _, y = panel.curves["field"].getData()
    assert list(y) == [0.5, 0.5, 0.5]


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #

def test_layouts_round_trip(suite, tmp_path):
    import apps.control_panel as cp
    panel = suite.control
    assert _tick(panel, "field")
    assert _tick(panel, "rf_freq")

    panel.layout_combo.setEditText("alignment")
    panel._save_layout()
    assert "alignment" in panel.layouts
    assert set(cp.layout_pids(panel.layouts["alignment"])) == {"field", "rf_freq"}

    # clear the selection, then bring it back
    panel._load_layout()
    assert set(panel.selected_pids()) == {"field", "rf_freq"}

    panel._delete_layout()
    assert "alignment" not in panel.layouts


def test_loading_a_layout_reports_parameters_that_are_not_connected(suite):
    """Half a panel with no explanation is worse than a line saying why."""
    notes = []
    panel = suite.control
    panel.on_log = notes.append
    panel.layouts["fmr"] = ["field", "clMag.nonexistent", "piezo.position_x"]
    panel.layout_combo.setEditText("fmr")
    panel._load_layout()

    assert any("not available" in n for n in notes), notes
    assert "field" in panel.widgets          # what IS there still appears


# --------------------------------------------------------------------------- #
# Kind markers in the tree, and hidden traces kept by layouts (2026-09-25)
# --------------------------------------------------------------------------- #

def _tree_node(panel, pid):
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value():
        if it.value().data(0, QtCore.Qt.UserRole) == pid:
            return it.value()
        it += 1
    return None


def _tree_kind(panel, pid):
    import apps.control_panel as cp
    node = _tree_node(panel, pid)
    return None if node is None else node.data(0, cp.KIND_ROLE)


def test_simulated_items_are_marked_control_or_indicator(suite):
    """No manifest: settables are controls, gettables are indicators."""
    panel = suite.control
    assert _tree_kind(panel, "field") == "control"
    assert _tree_kind(panel, "lockin_r") == "indicator"
    assert "SET" in _tree_node(panel, "field").toolTip(0)
    assert "READ-ONLY" in _tree_node(panel, "lockin_r").toolTip(0)


def _click_legend(panel, pid):
    """Click a trace's legend entry the way the operator does."""
    legend = panel.plot.plotItem.legend
    curve = panel.curves[pid]
    for sample, _label in legend.items:
        if sample.item is curve:
            class Ev:
                def button(self):
                    return QtCore.Qt.MouseButton.LeftButton

                def accept(self):
                    pass
            sample.mouseClickEvent(Ev())
            return
    raise AssertionError(f"{pid} has no legend entry")


def test_a_layout_remembers_hidden_traces(suite):
    import apps.control_panel as cp
    panel = suite.control
    for pid in ("lockin_r", "lockin_x", "aux_in"):
        assert _tick(panel, pid)
    _click_legend(panel, "lockin_x")                 # hide one trace
    assert not panel.curves["lockin_x"].isVisible()

    panel.layout_combo.setEditText("quiet")
    panel._save_layout()
    assert cp.layout_hidden(panel.layouts["quiet"]) == ["lockin_x"]
    # and it really reached the file
    import json
    on_disk = json.loads(cp.LAYOUTS_PATH.read_text(encoding="utf-8"))
    assert on_disk["quiet"]["hidden"] == ["lockin_x"]

    _click_legend(panel, "lockin_x")                 # show it again ...
    _click_legend(panel, "aux_in")                   # ... and hide another
    panel._load_layout()
    assert not panel.curves["lockin_x"].isVisible(), "hidden trace came back"
    assert panel.curves["aux_in"].isVisible(), "the layout did not hide aux_in"
    assert panel.curves["lockin_r"].isVisible()


def test_ticking_another_parameter_keeps_hidden_traces_hidden(suite):
    """Every tick rebuilds the plot; a hidden trace must not reappear."""
    panel = suite.control
    assert _tick(panel, "lockin_r")
    assert _tick(panel, "lockin_x")
    _click_legend(panel, "lockin_r")
    assert _tick(panel, "aux_in")
    assert not panel.curves["lockin_r"].isVisible()
    assert panel.curves["aux_in"].isVisible()


def test_an_old_layout_without_hidden_traces_shows_everything(suite):
    """suite_layouts.json from before 2026-09-25 is a plain list per layout."""
    panel = suite.control
    assert _tick(panel, "lockin_r")
    _click_legend(panel, "lockin_r")                 # hidden before the load
    panel.layouts["old"] = ["lockin_r", "lockin_x"]
    panel.layout_combo.setEditText("old")
    panel._load_layout()
    assert set(panel.selected_pids()) == {"lockin_r", "lockin_x"}
    assert all(c.isVisible() for c in panel.curves.values())
