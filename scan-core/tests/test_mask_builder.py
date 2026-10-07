"""The XY MASK card in the Scan Builder.

Shown when the scan has two axes that move something; when switched on it
becomes the recipe's `mask` block, survives a save and a load, previews a mask
FILE on the scan's own grid, and the scan run from it measures only inside it.
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
    yield win
    win.close()


def test_hidden_for_one_axis_shown_for_xy(builder):
    builder.add_axis("field")
    builder._rebuild_summary()
    assert builder.mask_card.isHidden()
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    assert not builder.mask_card.isHidden()
    assert builder.build_recipe().mask is None              # shown, but off


def test_measured_mask_round_trip_and_run(builder, tmp_path):
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    card = builder.mask_card
    card.enable.setChecked(True)
    card.det_box.setCurrentIndex(card.det_box.findData("reflectivity"))
    card.step_spin.setValue(3)
    r = builder.build_recipe()
    assert r.mask == {"detector": "reflectivity", "step": 3, "keep": "above",
                      "threshold": "auto", "margin": "auto"}
    assert r.validate(builder.registry) == []
    assert "XY mask pass 1: reflectivity every 3. point" in builder.detail.text()
    path = tmp_path / "m.yaml"
    r.save(path)
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    assert builder.build_recipe().mask is None
    builder.load_recipe(Recipe.load(path))
    assert builder.build_recipe().mask == r.mask
    ds = run(builder.build_recipe(), build_sim_registry())
    keep = ds.scan_mask.values.astype(bool)
    assert 0 < keep.sum() < keep.size


def test_a_picture_previews_and_places(builder, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    img = np.zeros((10, 10), np.uint8)
    img[:, :5] = 255                                        # the LEFT half
    p = tmp_path / "half.png"
    Image.fromarray(img).save(p)
    builder.load_recipe(Recipe(axes=[RASTER], detectors=["lockin_r"]))
    card = builder.mask_card
    card.enable.setChecked(True)
    card.source_box.setCurrentIndex(card.source_box.findData("file"))
    card.file_edit.setText(str(p))
    card._edited()
    assert not card.extent_box.isHidden() and card.step_spin.isHidden()
    card.preview()
    assert card.picture.pixmap() is not None and not card.picture.pixmap().isNull()
    assert "of 961 XY points measured" in card.info.text()
    r = builder.build_recipe()
    assert r.mask["from"] == str(p) and "detector" not in r.mask
    card.extent_box.setChecked(True)
    for w, v in zip(card.ext, (0.0, 45.0, -45.0, 45.0)):
        w.setValue(v)
    r = builder.build_recipe()
    assert r.mask["extent"] == {"x": [0.0, 45.0], "y": [-45.0, 45.0]}
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
    card = builder.mask_card
    card.enable.setChecked(True)
    card.source_box.setCurrentIndex(card.source_box.findData("file"))
    card.file_edit.setText(str(tmp_path / "nothing.png"))
    card.preview()
    assert "no preview" in card.info.text() and "does not exist" in card.info.text()


def test_two_linear_axes_get_x_and_y_choices(builder):
    builder.add_axis("pos_y")
    builder.add_axis("pos_x")
    builder._rebuild_summary()
    card = builder.mask_card
    assert not card.isHidden() and not card.x_box.isHidden()
    card.enable.setChecked(True)
    card.det_box.setCurrentIndex(card.det_box.findData("reflectivity"))
    r = builder.build_recipe()
    assert r.mask["axes"] == ["pos_x", "pos_y"]
    assert r.validate(builder.registry) == []
