"""SEGMENTS axes (2026-10-14): sweep up AND down, recorded per point.

Lukas, for hysteresis: "scan up and down ... stacking scanning arrays ...
type them manually". A `segments` axis is several linear runs visited in
order; a joint shared by two runs is measured once; the file says which run
every point belongs to (`<dim>_segment`, `<dim>_direction`, `segments_json`
-- the contract in docs/DEVELOPER_NOTES.md 4l), and a typed list that turns
back gets the same coordinates, split at its reversals.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run          # noqa: E402
from scan_core.autosave import write_dataset                   # noqa: E402
from scan_core.data import load                                # noqa: E402
from scan_core.recipe import (reversal_segments, segment_problems,  # noqa: E402
                              segment_values)

LOOP = [{"start": 0, "stop": 100, "num": 11},
        {"start": 100, "stop": -100, "num": 21},
        {"start": -100, "stop": 100, "num": 21}]


def _recipe(segments=LOOP, **extra):
    return Recipe(axes=[{"type": "linear", "param": "field", "segments": segments,
                         **extra}], detectors=["kerr"])


# ───────────────────────────── values and order ───────────────────────────

def test_values_in_order_and_the_joint_is_measured_once():
    vals, seg, info = segment_values(LOOP)
    # 11 up, then 20 down (100 is not measured twice), then 20 up
    assert len(vals) == 11 + 20 + 20
    assert np.allclose(vals[:11], np.linspace(0, 100, 11))
    assert vals[11] == pytest.approx(90.0)          # one step after the joint
    assert vals[30] == pytest.approx(-100.0)
    assert vals[31] == pytest.approx(-90.0)
    assert vals[-1] == pytest.approx(100.0)
    assert list(np.bincount(seg)) == [11, 20, 20]
    assert [d["direction"] for d in info] == [1, -1, 1]
    assert [d["joined"] for d in info] == [0, 1, 1]
    assert [d["first"] for d in info] == [0, 11, 31]
    assert [d["count"] for d in info] == [11, 20, 20]


def test_a_segment_that_does_not_start_at_the_last_end_keeps_its_first_point():
    vals, seg, info = segment_values([{"start": 0, "stop": 10, "num": 3},
                                      {"start": 20, "stop": 0, "num": 3}])
    assert list(vals) == [0, 5, 10, 20, 10, 0]
    assert [d["joined"] for d in info] == [0, 0]


def test_log_per_segment():
    vals, _, info = segment_values([{"start": 1, "stop": 100, "num": 3, "spacing": "log"},
                                    {"start": 100, "stop": 1, "num": 3, "spacing": "log"}])
    assert np.allclose(vals, [1, 10, 100, 10, 1])
    assert info[0]["spacing"] == "log"


def test_compile_gives_the_values_and_the_segments():
    (d,) = _recipe().compile().dims
    assert d.size == 51 and d.segment_source == "recipe"
    assert list(np.unique(d.segment)) == [0, 1, 2]


# ───────────────────────────── validation ─────────────────────────────────

@pytest.mark.parametrize("segments, words", [
    ([{"start": 0, "stop": 10, "num": 1}], "at least 2"),
    ([{"start": 5, "stop": 5, "num": 3}], "from and to are equal"),
    ([{"start": 0, "stop": float("inf"), "num": 3}], "finite"),
    ([{"start": 0, "stop": float("nan"), "num": 3}], "finite"),
    ([], "non-empty"),
    ([{"start": -1, "stop": 10, "num": 3, "spacing": "log"}], "same sign"),
])
def test_bad_segments_are_refused_in_plain_words(segments, words):
    assert any(words in e for e in segment_problems(segments))
    errs = _recipe(segments).validate(build_sim_registry())
    assert any(words in e for e in errs), errs
    assert not any(e.startswith("compile error") for e in errs)   # said once


def test_a_good_loop_validates():
    assert _recipe().validate(build_sim_registry()) == []


def test_segments_beside_start_stop_or_on_a_fly_axis_are_refused():
    errs = _recipe(start=0).validate(build_sim_registry())
    assert any("either segments or start" in e for e in errs)
    r = Recipe(axes=[{"type": "fly", "param": "pos_x", "segments": LOOP, "speed": 10}],
               detectors=["kerr"])
    assert any("not on a fly axis" in e for e in r.validate(build_sim_registry()))


def test_an_enum_axis_cannot_use_segments():
    from scan_core.registry import Settable
    reg = build_sim_registry()
    reg.add(Settable("lockin.input", "Input", "", (float("-inf"), float("inf")),
                     lambda v: None, lambda: "A", dtype="enum", options=["A", "B"]))
    r = Recipe(axes=[{"type": "linear", "param": "lockin.input",
                      "segments": [{"start": 0, "stop": 1, "num": 2}]}],
               detectors=["kerr"])
    assert any("not a number" in e for e in r.validate(reg))


def test_limits_are_checked_over_every_segment():
    r = _recipe([{"start": 0, "stop": 100, "num": 3}, {"start": 100, "stop": -300, "num": 3}])
    assert any("exceeds limits" in e for e in r.validate(build_sim_registry()))


# ───────────────────────────── the data file ──────────────────────────────

def test_the_dataset_carries_the_segments_contract(tmp_path):
    reg = build_sim_registry()
    ds = run(_recipe(), reg)
    assert ds.sizes["field"] == 51
    seg, dirn = ds["field_segment"], ds["field_direction"]
    assert seg.dims == ("field",) and dirn.dims == ("field",)
    assert seg.dtype == np.int32 and dirn.dtype == np.int8
    assert list(np.bincount(seg.values)) == [11, 20, 20]
    assert set(dirn.values[seg.values == 1]) == {-1}
    info = json.loads(ds["field"].attrs["segments_json"])
    assert [d["direction"] for d in info] == [1, -1, 1]
    assert ds["field"].attrs["segment_source"] == "recipe"
    assert ds["field"].attrs["n_segments"] == 3
    # the hysteretic sim detector really is hysteretic: at +10 mT the up
    # branch (from -100) is still negative, the down branch (from +100) positive
    k, f = ds["kerr"].values, ds["field"].values
    up = (seg.values == 2) & np.isclose(f, 10)
    down = (seg.values == 1) & np.isclose(f, 10)
    assert k[up][0] < 0 < k[down][0]
    # ... and all of it survives the file
    path = write_dataset(ds, tmp_path / "loop.nc")
    back = load(path)
    assert np.array_equal(back["field_segment"].values, seg.values)
    assert np.array_equal(back["field_direction"].values, dirn.values)
    assert json.loads(back["field"].attrs["segments_json"]) == info
    # the recipe round-trips through the file too
    again = Recipe.from_dict(json.loads(back.attrs["recipe_json"]))
    assert again.axes[0]["segments"] == LOOP


def test_the_schema_describes_a_segments_axis():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parent.parent / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(_recipe().to_dict(), schema)
    bad = _recipe([{"start": 0, "stop": 1, "num": 1}]).to_dict()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_recipe_round_trips_through_yaml(tmp_path):
    r = _recipe()
    r.save(tmp_path / "loop.yaml")
    back = Recipe.load(tmp_path / "loop.yaml")
    assert back.axes[0]["segments"] == LOOP
    assert np.allclose(back.compile().dims[0].coord, r.compile().dims[0].coord)


def test_a_typed_list_that_turns_back_gets_segments(tmp_path):
    vals = [0, 50, 100, 50, 0, -50, -100, -50, 0]
    seg, info = reversal_segments(vals)
    assert list(seg) == [0, 0, 0, 1, 1, 1, 1, 2, 2]
    assert [d["direction"] for d in info] == [1, -1, 1]
    assert [d["joined"] for d in info] == [0, 1, 1]
    assert info[1]["start"] == 100 and info[1]["stop"] == -100
    # a monotonic list, text, or too short: nothing to split
    assert reversal_segments([0, 1, 5, 9]) is None
    assert reversal_segments(np.array(["a", "b", "a"], dtype=object)) is None
    # a repeated value does not turn the direction
    assert reversal_segments([0, 1, 1, 2]) is None
    r = Recipe(axes=[{"type": "array", "param": "field", "values": vals}],
               detectors=["kerr"])
    assert r.validate(build_sim_registry()) == []
    ds = run(r, build_sim_registry())
    assert list(ds["field_segment"].values) == list(seg)
    assert ds["field"].attrs["segment_source"] == "reversals"


def test_a_plain_axis_gets_no_segment_coordinates():
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 0, "stop": 10,
                      "num": 3}], detectors=["kerr"])
    ds = run(r, build_sim_registry())
    assert "field_segment" not in ds.coords
    assert "segments_json" not in ds["field"].attrs


# ───────────────────────────── the loop helpers ───────────────────────────

def test_return_sweep_and_loop_helpers():
    from scan_core.recipe import hysteresis_loop, return_sweep
    first = [{"start": 0, "stop": 100, "num": 11}]
    assert return_sweep(first)[1] == {"start": 100.0, "stop": 0.0, "num": 11}
    # a virgin curve from 0 is mirrored through zero, at the same step
    assert hysteresis_loop(first) == [{"start": 0, "stop": 100, "num": 11},
                                      {"start": 100.0, "stop": -100.0, "num": 21},
                                      {"start": -100.0, "stop": 100.0, "num": 21}]
    # a range a -> b: back to a and up again
    assert hysteresis_loop([{"start": -50, "stop": 50, "num": 5}])[1:] == [
        {"start": 50.0, "stop": -50.0, "num": 5}, {"start": -50.0, "stop": 50.0, "num": 5}]
    assert return_sweep([]) == [] and hysteresis_loop([]) == []


# ───────────────────────────── the builder ────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _builder():
    from apps.scan_builder import ScanBuilder
    return ScanBuilder(registry=build_sim_registry())


def test_builder_segments_spacing_buttons_and_count(qapp):
    b = _builder()
    b.add_axis("field")
    row = b.rows[-1]
    row.start.setValue(0); row.stop.setValue(100); row.num.setValue(11)
    row.set_spacing("segments")
    # switching keeps what from / to / pts said, as the first run
    assert row.seg_editor.segments() == [{"start": 0, "stop": 100, "num": 11}]
    assert row.range_w.isHidden() and not row.seg_w.isHidden()
    assert not row.fly.isEnabled() if hasattr(row, "fly") else True
    row.seg_editor.add_return()
    assert row.seg_editor.segments()[1] == {"start": 100, "stop": 0, "num": 11}
    assert "21 points in 2 segments" in row.seg_editor.count_lbl.text()
    assert "1 joint" in row.seg_editor.count_lbl.text()
    row.seg_editor.remove_row()                    # back to the first run
    row.seg_editor.add_loop()
    assert row.seg_editor.segments() == LOOP
    assert row.seg_editor.n_points() == 51
    assert "0 -> 100 -> -100 -> 100" in row.seg_btn.text() and "51 pts" in row.seg_btn.text()
    ax = row.to_axis()
    assert ax == {"type": "linear", "param": "field", "segments": LOOP}
    assert b.build_recipe().validate(b.registry) == []
    # a bad run is said in the count line, in red, and refused by validate
    row.seg_editor.table.item(1, 2).setText("1")
    assert "at least 2" in row.seg_editor.count_lbl.text()
    assert any("at least 2" in e for e in b.build_recipe().validate(b.registry))


def test_builder_table_rows_add_remove_move(qapp):
    from apps.scan_builder import SegmentsEditor
    ed = SegmentsEditor("mT")
    ed.set_segments([{"start": 0, "stop": 10, "num": 3}])
    ed.add_row()                                   # continues back from its end
    assert ed.segments()[1] == {"start": 10, "stop": 0, "num": 3}
    ed.table.selectRow(1)
    ed.move_row(-1)
    assert ed.segments()[0] == {"start": 10, "stop": 0, "num": 3}
    ed.table.selectRow(0)
    ed.remove_row()
    assert ed.segments() == [{"start": 0, "stop": 10, "num": 3}]
    ed.remove_row()                                # the last run stays
    assert len(ed.segments()) == 1


def test_builder_segments_round_trip(qapp, tmp_path):
    b = _builder()
    assert b.load_recipe(_recipe()) == []
    row = b.rows[0]
    assert row.raw is None and row.spacing() == "segments"
    assert row.to_axis() == {"type": "linear", "param": "field", "segments": LOOP}
    # the axis preview shows the order and marks the boundaries
    from apps.scan_builder import AxisPreviewDialog
    dlg = AxisPreviewDialog(row, b.registry)
    assert dlg.boundaries == [11, 31]
    assert "3 segments (up 11, down 20, up 20)" in dlg.header.text()
    heads = [dlg.table.horizontalHeaderItem(c).text() for c in range(dlg.table.columnCount())]
    assert "segment" in heads
    assert dlg.table.item(11, heads.index("segment")).text() == "2 down"
    assert "repeat a value" not in dlg.warn.text()     # up and down on purpose
    dlg.close()


def test_the_axis_is_said_as_its_path(qapp):
    from apps.scan_builder import _raw_axis_text
    from apps.scan_server_view import _axis_text
    ax = {"type": "linear", "param": "field", "segments": LOOP}
    assert "0 -> 100 -> -100 -> 100  (3 segments)" in _axis_text(ax)
    grid = {"type": "grid", "x": {"param": "field", "segments": LOOP},
            "y": {"param": "pos_y", "start": 0, "stop": 1, "num": 2}}
    assert "0 -> 100 -> -100 -> 100 (3 segments)" in _raw_axis_text(grid)


def test_a_loop_typed_as_a_list_validates_and_runs(qapp):
    b = _builder()
    b.add_axis("field")
    row = b.rows[-1]
    row.set_spacing("list")
    row.list_edit.setText("0, 50, 100, 50, 0, -50, -100, -50, 0")
    r = b.build_recipe()
    r.detectors = ["kerr"]
    assert r.validate(b.registry) == []
    ds = run(r, b.registry)
    assert list(ds["field_segment"].values) == [0, 0, 0, 1, 1, 1, 1, 2, 2]
