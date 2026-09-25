"""The Scan tab palette groups parameters under the service they belong to."""

import os

import pytest

if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import build_sim_registry
from scan_core.registry import Gettable, Registry, Settable


def _lab_like_registry():
    """What the suite builds from live services: every id is <module>.<id>."""
    reg = Registry()
    reg.add(Settable("pm16.wavelength", "Wavelength", "nm", (400, 1100),
                     set_fn=lambda v: None, get_fn=lambda: 520.0))
    reg.add(Settable("kim.position_x", "Position X", "um", (-1000, 1000),
                     set_fn=lambda v: None, get_fn=lambda: 0.0))
    reg.add(Settable("kim.position_y", "Position Y", "um", (-1000, 1000),
                     set_fn=lambda v: None, get_fn=lambda: 0.0))
    reg.add(Gettable("pm16.power", "Power", "mW", lambda: 1.0))
    reg.add(Gettable("pm16.power_std", "Power std. dev.", "mW", lambda: 0.0))
    reg.add(Gettable("kim.moving_x", "Moving X", "", lambda: False))
    return reg


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _tree(tree):
    from PySide6.QtCore import Qt
    out = {}
    for g in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(g)
        out[group.text(0)] = [group.child(i).data(0, Qt.UserRole)
                              for i in range(group.childCount())]
    return out


def test_split_id():
    from apps.scan_builder import split_id
    assert split_id("pm16.power") == ("pm16", "power")
    assert split_id("field") == ("", "field")


def test_axes_and_detectors_are_children_of_their_service(app):
    from apps.scan_builder import ScanBuilder
    win = ScanBuilder(build_sim_registry())
    try:
        win.set_registry(_lab_like_registry(),
                         group_names={"pm16": "Power meter", "kim": "Inertia stage"})
        assert _tree(win.set_tree) == {
            "Power meter  \u00b7  pm16   (1)": ["pm16.wavelength"],
            "Inertia stage  \u00b7  kim   (2)": ["kim.position_x", "kim.position_y"],
        }
        assert _tree(win.det_tree) == {
            "Power meter  \u00b7  pm16   (2)": ["pm16.power", "pm16.power_std"],
            "Inertia stage  \u00b7  kim   (1)": ["kim.moving_x"],
        }
    finally:
        win.close()


def test_heading_is_not_an_axis_and_leaves_still_work(app):
    from PySide6.QtCore import Qt
    from apps.scan_builder import ScanBuilder
    win = ScanBuilder(_lab_like_registry())
    try:
        kim = win.set_tree.topLevelItem(1)
        win._add_item(kim)                          # a service heading: ignored
        assert win.rows == []
        win._add_item(kim.child(0))                 # a parameter: becomes an axis
        assert [r.param.id for r in win.rows] == ["kim.position_x"]
        assert win.rows[0].limits_lbl.text().startswith("kim \u00b7 ")

        for it in win._det_items():
            it.setCheckState(0, Qt.Checked if it.data(0, Qt.UserRole) == "pm16.power"
                             else Qt.Unchecked)
        assert win.build_recipe().detectors == ["pm16.power"]
        heading = win.det_tree.topLevelItem(0)
        assert not heading.flags() & Qt.ItemIsUserCheckable
    finally:
        win.close()


def test_unprefixed_registry_is_one_simulator_branch(app):
    from apps.scan_builder import SIM_GROUP, ScanBuilder
    win = ScanBuilder(build_sim_registry())
    try:
        assert win.set_tree.topLevelItemCount() == 1
        assert win.set_tree.topLevelItem(0).text(0).startswith(SIM_GROUP)
    finally:
        win.close()
