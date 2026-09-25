"""Single-valued parameters: the conditions a measurement was taken under.

From the rig, 2026-09-16: "can we add possibility to add single valued
parameters? ... I want to define the measurement condition in the saved script."

A condition is not an axis -- it never moves -- but it is every bit as much part
of what the measurement IS. It has to be settable in the builder, applied before
the first point, saved inside the definition, restored when that definition is
loaded, and visible in the data file without parsing a JSON blob.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run                  # noqa: E402


def _recipe(**kw) -> Recipe:
    base = dict(name="t", fixed={"rf_power": 5.0},
                detectors=["lockin_r"],
                axes=[{"type": "linear", "param": "field",
                       "start": 0, "stop": 10, "num": 3}])
    base.update(kw)
    return Recipe(**base)


# ──────────────────────────── the engine's end ────────────────────────────────

def test_a_condition_is_applied_before_the_first_point():
    reg = build_sim_registry()
    reg.get("rf_power").set(-30.0)
    run(_recipe(), reg, created_iso="t")
    assert reg.get("rf_power").get() == 5.0


def test_a_condition_is_a_coordinate_in_the_data_file():
    """Not only inside recipe_json: a scalar coord is visible in ncdump, in
    MATLAB and in the viewer's header, which is where the question is asked."""
    reg = build_sim_registry()
    ds = run(_recipe(), reg, created_iso="t")
    assert "rf_power" in ds.coords
    assert float(ds.coords["rf_power"]) == 5.0
    assert ds.coords["rf_power"].attrs["units"] == "dBm"
    assert ds.coords["rf_power"].attrs["fixed"] == "true"
    assert ds["lockin_r"].dims == ("field",), "it must not become a dimension"


def test_a_condition_survives_a_round_trip_through_netcdf(tmp_path):
    reg = build_sim_registry()
    ds = run(_recipe(), reg, created_iso="t")
    path = tmp_path / "m.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    import xarray as xr
    with xr.open_dataset(path, engine="h5netcdf") as back:
        assert float(back.coords["rf_power"]) == 5.0
        assert Recipe.from_dict(__import__("json").loads(
            back.attrs["recipe_json"])).fixed["rf_power"] == 5.0


def test_the_recipe_carries_conditions_through_yaml(tmp_path):
    path = tmp_path / "scan.yaml"
    _recipe().save(path)
    assert Recipe.load(path).fixed == {"rf_power": 5.0}


# ──────────────────────────────── validation ──────────────────────────────────

def test_a_condition_outside_its_limits_is_refused():
    reg = build_sim_registry()
    errs = _recipe(fixed={"rf_power": 500.0}).validate(reg)
    assert any("outside its limits" in e for e in errs)


def test_a_parameter_cannot_be_held_and_swept_at_once():
    """It would run -- set once, then swept -- but the condition written into
    the file would be a value the measurement spent no time at."""
    reg = build_sim_registry()
    errs = _recipe(fixed={"field": 5.0}).validate(reg)
    assert any("both a condition and an axis" in e for e in errs)


def test_an_unknown_condition_is_named():
    reg = build_sim_registry()
    errs = _recipe(fixed={"nope.power": 1.0}).validate(reg)
    assert any("unknown parameter 'nope.power'" in e for e in errs)


def test_a_detector_cannot_be_a_condition():
    reg = build_sim_registry()
    errs = _recipe(fixed={"lockin_r": 1.0}).validate(reg)
    assert any("not settable" in e for e in errs)


# ────────────────────────────────── the GUI ───────────────────────────────────

@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    yield win
    win.close()


def test_a_condition_can_be_added_and_reaches_the_recipe(builder):
    row = builder.add_fixed("rf_power")
    row.value_box.setValue(3.0)
    assert builder.build_recipe().fixed == {"rf_power": 3.0}
    assert "RF power = 3 dBm" in builder.detail.text()


def test_adding_the_same_condition_twice_edits_the_one_that_is_there(builder):
    """Two rows for one parameter would send two setpoints and keep the last."""
    first = builder.add_fixed("rf_power", 1.0)
    again = builder.add_fixed("rf_power", 7.0)
    assert again is first
    assert len(builder.fixed_rows) == 1
    assert builder.build_recipe().fixed == {"rf_power": 7.0}


def test_conditions_come_back_when_the_definition_is_loaded(builder):
    missing = builder.load_recipe(_recipe(fixed={"rf_power": -4.0, "device_v": 2.0}))
    assert missing == []
    assert builder.build_recipe().fixed == {"rf_power": -4.0, "device_v": 2.0}
    assert [r.param.id for r in builder.fixed_rows] == ["rf_power", "device_v"]


def test_a_condition_the_registry_does_not_have_is_flagged_not_dropped(builder):
    """Same rule as axes: a definition written against other instruments must
    say what is missing rather than quietly measure something else."""
    missing = builder.load_recipe(_recipe(fixed={"hf2.tc1": 0.01}))
    assert "hf2.tc1" in missing
    assert builder.fixed_rows == []


def test_loading_replaces_the_conditions_rather_than_adding_to_them(builder):
    builder.add_fixed("rf_power", 9.0)
    builder.load_recipe(_recipe(fixed={"device_v": 1.0}))
    assert builder.build_recipe().fixed == {"device_v": 1.0}


def test_an_out_of_range_condition_shows_as_invalid_in_the_summary(builder):
    """The spin box clamps, so this is really a check that a definition LOADED
    with an impossible value is reported instead of silently corrected."""
    builder.add_axis("field")
    row = builder.add_fixed("rf_power")
    row.value_box.setRange(-1e6, 1e6)       # as if the limit had moved under us
    row.value_box.setValue(900.0)
    assert "invalid" in builder.summary.text()
    assert "outside its limits" in builder.detail.text()


def test_a_scan_runs_with_its_conditions(builder):
    builder.add_axis("field")
    builder.rows[0].num.setValue(3)
    builder.add_fixed("rf_power", 6.0)
    builder.per_pt.setValue(0.0)
    builder.run_scan(block=True)
    assert builder.registry.get("rf_power").get() == 6.0
    assert float(builder.dataset.coords["rf_power"]) == 6.0


# ─────────────────────────── indentation of the loops ─────────────────────────

def test_the_axis_stack_is_indented_by_loop_depth(builder):
    """Which axis is the slow one is the thing operators misread; a number in a
    column is easy to skim past, a staircase is not."""
    for pid in ("field", "rf_freq", "pos_x"):
        builder.add_axis(pid)
    lefts = [r.layout().getContentsMargins()[0] for r in builder.rows]
    assert lefts == sorted(lefts) and lefts[0] < lefts[-1]
    assert [r.level_lbl.text() for r in builder.rows] == ["0", "1", "2"]
    assert "OUTERMOST" in builder.rows[0].toolTip()


def test_reordering_re_indents(builder):
    builder.add_axis("field")
    builder.add_axis("rf_freq")
    before = builder.rows[0].layout().getContentsMargins()[0]
    builder._move_row(builder.rows[0], +1)          # field becomes the inner one
    assert builder.rows[1].param.id == "field"
    assert builder.rows[1].layout().getContentsMargins()[0] > before


def test_the_indent_is_capped_so_a_deep_stack_still_fits(builder):
    from apps.scan_builder import AxisRow
    for pid in ("field", "rf_freq", "rf_phase", "device_v", "pos_x", "pos_y"):
        builder.add_axis(pid)
    lefts = [r.layout().getContentsMargins()[0] for r in builder.rows]
    assert max(lefts) == 10 + AxisRow.INDENT_PX * AxisRow.INDENT_MAX
    assert lefts[-1] == lefts[-2] == max(lefts)
