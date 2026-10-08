"""The SCOUT PASS in the Scan Builder (2026-10-08; it replaced the XY MASK card).

Lukas: the mask card "awkwardly jumps in place when you get two axes". The
section is now ALWAYS there, one line at the bottom of the axis stack when
closed, and the axes to scout are ticked on the axis rows. Pinned here: it
neither appears nor disappears nor changes height with the number of axes;
ticks + section become the recipe's `scout` block and survive a save and a
load (also an OLD `mask` block); the grid preview lists the scout's points;
a mask FILE previews on the scan's own grid; a run from the builder measures
only inside the mask; the run pane shows the scout's progress and threshold.
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

RASTER = {"type": "raster",
          "x": {"param": "pos_x", "start": -45, "stop": 45, "num": 31},
          "y": {"param": "pos_y", "start": -45, "stop": 45, "num": 31}}


@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    win.resize(1500, 950)
    win.show()
    yield win
    win.close()


def _pump():
    from PySide6 import QtWidgets
    for _ in range(5):
        QtWidgets.QApplication.processEvents()


def _row(builder, pid):
    return next(r for r in builder.rows if r.param.id == pid)


def test_the_section_is_always_there_and_does_not_jump(builder):
    sec = builder.scout_section
    heights, tops = [], []
    for pid in (None, "field", "rf_freq", "pos_x"):
        if pid:
            builder.add_axis(pid)
        builder._rebuild_summary()
        _pump()
        assert sec.isVisible(), pid
        assert not sec.expanded()
        heights.append(sec.height())
        # it sits at the bottom of the axis stack card, whatever is in it
        tops.append(sec.mapTo(builder, sec.rect().topLeft()).y())
    assert len(set(heights)) == 1, heights
    assert len(set(tops)) == 1, tops
    assert "tick 'scout' on the axes" in sec.state.text()
    assert builder.build_recipe().scout is None              # nothing ticked = off


def test_opening_it_takes_room_from_the_axis_list_only(builder):
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    _pump()
    below = builder.routines_card.mapTo(builder, builder.routines_card.rect().topLeft()).y()
    builder.scout_section.set_expanded(True)
    _pump()
    assert builder.scout_section.scroll.isVisible()
    after = builder.routines_card.mapTo(builder, builder.routines_card.rect().topLeft()).y()
    assert after == below                                    # the cards below stay put
    # ... and the axis list still shows 2.5 axis rows: the axes being scouted
    rows = builder.AXIS_ROWS_MIN * builder.AXIS_ROW_PX
    assert builder.axis_scroll.height() >= rows - 1
    builder.scout_section.set_expanded(False)
    _pump()
    assert builder.routines_card.mapTo(builder, builder.routines_card.rect().topLeft()).y() == below


def test_ticking_a_raster_scouts_it_and_round_trips(builder, tmp_path):
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    row = builder.rows[0]
    row.scout.setChecked(True)
    row.scout_step.setValue(3)
    sec = builder.scout_section
    sec.det_box.setCurrentIndex(sec.det_box.findData("reflectivity"))
    r = builder.build_recipe()
    assert r.scout == {"axes": {"pos_x": 3, "pos_y": 3}, "detector": "reflectivity",
                       "keep": "above", "threshold": "auto", "margin": "auto"}
    assert r.validate(builder.registry) == []
    assert "scout 121 pts first" in builder.summary.text()
    assert "decided by the scout" in builder.detail.text()
    path = tmp_path / "s.yaml"
    r.save(path)
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    assert builder.build_recipe().scout is None
    builder.load_recipe(Recipe.load(path))
    assert builder.rows[0].is_scout()
    assert builder.build_recipe().scout == r.scout
    ds = run(builder.build_recipe(), build_sim_registry())
    keep = ds.scan_mask.values.astype(bool)
    assert 0 < keep.sum() < keep.size


def test_an_old_mask_recipe_ticks_its_raster(builder):
    old = Recipe(axes=[RASTER], detectors=["lockin_r"],
                 mask={"detector": "reflectivity", "step": 4, "keep": "below"})
    assert builder.load_recipe(old) == []
    assert builder.rows[0].is_scout() and builder.rows[0].scout_step.value() == 4
    r = builder.build_recipe()
    assert r.scout["axes"] == {"pos_x": 4, "pos_y": 4} and r.scout["keep"] == "below"


def test_two_linear_axes_put_x_the_inner_one_first(builder):
    builder.add_axis("pos_y")
    builder.add_axis("pos_x")
    for pid in ("pos_y", "pos_x"):
        _row(builder, pid).scout.setChecked(True)
    sec = builder.scout_section
    sec.det_box.setCurrentIndex(sec.det_box.findData("reflectivity"))
    r = builder.build_recipe()
    assert list(r.scout["axes"]) == ["pos_x", "pos_y"]
    assert r.validate(builder.registry) == []


def test_any_axes_deviates_each_and_settings(builder, tmp_path):
    builder.add_axis("field")
    builder.add_axis("rf_freq")
    builder.add_axis("device_v")
    _row(builder, "rf_freq").scout.setChecked(True)
    sec = builder.scout_section
    sec.det_box.setCurrentIndex(sec.det_box.findData("lockin_r"))
    sec.keep_box.setCurrentIndex(sec.keep_box.findData("deviates"))
    assert not sec.k_spin.isHidden() and sec.thr_box.isHidden()
    assert sec.outer_box.isEnabled()                          # field is outside rf_freq
    sec.outer_box.setCurrentIndex(sec.outer_box.findData("each"))
    sec.add_scout_setting("rf_power", 7.0)
    r = builder.build_recipe()
    assert r.scout == {"axes": {"rf_freq": 3}, "detector": "lockin_r",
                       "keep": "deviates", "k": 4.0, "margin": "auto",
                       "per_outer": "each", "settings": {"rf_power": 7.0}}
    assert r.validate(builder.registry) == []
    path = tmp_path / "e.yaml"
    r.save(path)
    builder.load_recipe(Recipe(axes=[], detectors=["lockin_r"]))
    builder.load_recipe(Recipe.load(path))
    assert builder.build_recipe().scout == r.scout
    # the grid preview: the scout's indices, the last one included, and
    # what the other axes do during the scout
    sec.set_expanded(True)
    text = sec.info.text()
    assert "rf_freq:" in text and "18, 20" in text            # 21 points, every 3rd
    assert "x 21 (again at every field)" in text
    assert "device_v" in text and "first value" in text
    assert sec.picture.pixmap() is not None and not sec.picture.pixmap().isNull()


def test_a_picture_previews_and_places(builder, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    img = np.zeros((10, 10), np.uint8)
    img[:, :5] = 255                                        # the LEFT half
    p = tmp_path / "half.png"
    Image.fromarray(img).save(p)
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    builder.rows[0].scout.setChecked(True)
    sec = builder.scout_section
    sec.source_box.setCurrentIndex(sec.source_box.findData("picture"))
    sec.file_edit.setText(str(p))
    sec._edited()
    assert not sec.extent_box.isHidden() and sec.det_box.isHidden()
    sec.preview_mask()
    assert sec.picture.pixmap() is not None and not sec.picture.pixmap().isNull()
    assert "of 961 points measured" in sec.info.text()
    r = builder.build_recipe()
    assert r.scout["from"] == str(p) and "detector" not in r.scout
    sec.extent_box.setChecked(True)
    for w, v in zip(sec.ext, (0.0, 45.0, -45.0, 45.0)):
        w.setValue(v)
    r = builder.build_recipe()
    assert r.scout["extent"] == {"x": [0.0, 45.0], "y": [-45.0, 45.0]}
    ds = run(r, build_sim_registry())
    keep = ds.scan_mask.values.astype(bool)
    x = ds.pos_x.values
    # the left half of the picture now lies on 0..22.5 um; left of 0 is
    # OUTSIDE the picture and therefore measured
    assert keep[:, x < -1].all()
    assert keep[:, (x > 2) & (x < 20)].all()
    assert not keep[:, x > 26].any()


def test_a_bad_file_says_why(builder, tmp_path):
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    builder.rows[0].scout.setChecked(True)
    sec = builder.scout_section
    sec.source_box.setCurrentIndex(sec.source_box.findData("picture"))
    sec.file_edit.setText(str(tmp_path / "nothing.png"))
    sec.preview_mask()
    assert "no preview" in sec.info.text() and "does not exist" in sec.info.text()


def test_the_run_pane_shows_the_scout(builder):
    builder.load_recipe(Recipe.load(Path(__file__).parents[1] / "recipes" / "scout_xy.yaml"))
    builder._running = True
    builder._scout_note, builder._scout_var, builder._scout_prev = "", None, ""
    builder._on_scout({"phase": "scout", "done": 5, "total": 441, "eta_s": 30.0,
                       "where": "", "variable": "mask_reflectivity"})
    assert builder.progress.format().startswith("SCOUT: 5/441 points")
    builder._on_scout({"phase": "made", "where": "", "variable": "mask_reflectivity",
                       "threshold": 0.5013, "keep": "above", "unit": "", "kept": 1262,
                       "of": 3721, "measured": 1262, "total": 3721})
    assert "threshold 0.5013 -> 1,262 of 3,721 points (34 %)" in builder.detail.text()
    builder._running = False
