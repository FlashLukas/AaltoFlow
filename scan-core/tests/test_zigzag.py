"""Zig-zag (serpentine) order: sweep every other pass of an inner axis backwards,
so the stage does not fly back to the start of each row.

The point of the tests: the ORDER of visiting changes, the DATA does not.
"""

import numpy as np
import pytest

from scan_core.engine import _unravel, _zigzag, run
from scan_core.recipe import Recipe
from scan_core.registry import Gettable, Registry, Settable


def _recording_registry():
    """x and y settables that remember every value they were given."""
    state = {"x": 0.0, "y": 0.0}
    order = []
    reg = Registry()

    def make(name):
        def setter(v, _n=name):
            state[_n] = v
            order.append((_n, v))
        return setter

    reg.add(Settable("x", "X", "", (0, 10), set_fn=make("x"), get_fn=lambda: state["x"]))
    reg.add(Settable("y", "Y", "", (0, 10), set_fn=make("y"), get_fn=lambda: state["y"]))
    # a detector that tells you exactly where it was measured
    reg.add(Gettable("xy", "X+10Y", "", lambda: state["x"] + 10 * state["y"]))
    return reg, order


def _recipe(zigzag: bool) -> Recipe:
    return Recipe(name="zz", zigzag=zigzag, detectors=["xy"], axes=[
        {"type": "linear", "param": "y", "start": 0, "stop": 2, "num": 3},   # outer
        {"type": "linear", "param": "x", "start": 0, "stop": 2, "num": 3}])  # inner


def test_zigzag_reverses_every_other_row():
    idx = [_zigzag(_unravel(f, (3, 3)), (3, 3)) for f in range(9)]
    assert idx == [(0, 0), (0, 1), (0, 2),      # row 0 forwards
                   (1, 2), (1, 1), (1, 0),      # row 1 BACKWARDS
                   (2, 0), (2, 1), (2, 2)]      # row 2 forwards again


def test_the_stage_does_not_fly_back_between_rows():
    reg, order = _recording_registry()
    run(_recipe(zigzag=True), reg, created_iso="t")
    xs = [v for name, v in order if name == "x"]
    assert xs == [0, 1, 2, 1, 0, 1, 2]          # no jump back to 0 at a row end
    # ... which the ordinary order DOES do, twice
    reg2, order2 = _recording_registry()
    run(_recipe(zigzag=False), reg2, created_iso="t")
    xs2 = [v for name, v in order2 if name == "x"]
    assert xs2 == [0, 1, 2, 0, 1, 2, 0, 1, 2]


def test_the_data_is_identical_either_way():
    """Every point is stored at its own coordinate, so the cube must match."""
    reg_a, _ = _recording_registry()
    straight = run(_recipe(zigzag=False), reg_a, created_iso="t")
    reg_b, _ = _recording_registry()
    zig = run(_recipe(zigzag=True), reg_b, created_iso="t")
    np.testing.assert_array_equal(straight["xy"].values, zig["xy"].values)
    # and the values really are x + 10y at each (y, x)
    assert zig["xy"].values.tolist() == [[0, 1, 2], [10, 11, 12], [20, 21, 22]]


def test_off_by_default_and_carried_in_the_definition():
    assert Recipe().zigzag is False
    assert Recipe.from_dict(Recipe(zigzag=True).to_dict()).zigzag is True
    assert Recipe.from_dict({"axes": []}).zigzag is False       # an older file


def test_the_builder_offers_it_unticked():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    from scan_core import build_sim_registry

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    try:
        assert win.zigzag_box.isChecked() is False
        assert win.build_recipe().zigzag is False
        win.zigzag_box.setChecked(True)
        assert win.build_recipe().zigzag is True
        assert "zig-zag" in win.detail.text() or not win.rows
        win.load_recipe(Recipe(axes=[], detectors=[], zigzag=False))
        assert win.zigzag_box.isChecked() is False      # loading restores it
    finally:
        win.close()
