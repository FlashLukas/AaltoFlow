"""The newest camera frame next to the map (apps/image_live.py), offscreen.

Shown only while an image detector is recorded; the frame comes from the
engine (also for a big map whose frames are only in the file), or -- for a
dataset that did not come from this engine (a mirror, a file) -- from the
last point the per-point mask marks as measured.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from scan_core import framestore as FS                       # noqa: E402
from scan_core.autosave import write_dataset                 # noqa: E402
from scan_core.data import load                              # noqa: E402
from scan_core.engine import run                             # noqa: E402

from tests.test_image_detector import H, W, _camera_registry, _frame_for, _recipe  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_hidden_without_images_shown_with_the_newest_frame(qapp, tmp_path):
    from apps.image_live import LiveImage, newest_frame
    w = LiveImage()
    reg, _ = _camera_registry()
    seen = []

    def on_point(done, total, snap):
        ds = snap()
        w.set_dataset(ds)
        seen.append((done, newest_frame(ds, "cam.image")[0]))
    ds = run(_recipe(n=3), reg, created_iso="t", on_point=on_point)
    assert not w.isHidden()
    assert [s[1] for s in seen] == [(0,), (1,), (2,)]
    assert "point [2]" in w.caption.text() and f"{W} x {H} px" in w.caption.text()
    assert w.img.image is not None and w.img.image.shape == (H, W)

    # a file (no engine attached): the last point the mask marks
    path = write_dataset(ds, tmp_path / "f.nc")
    back = load(path)
    try:
        idx, frame = newest_frame(back, "cam.image")
        assert idx == (2,) and np.array_equal(frame, _frame_for(2.0))
    finally:
        back.close()

    # a dataset without any image hides the picture
    import xarray as xr
    w.set_dataset(xr.Dataset({"a": ("x", [1.0, 2.0])}))
    assert w.isHidden()


def test_a_big_map_shows_the_frame_that_is_only_in_the_file(qapp, tmp_path, monkeypatch):
    from apps.image_live import LiveImage
    monkeypatch.setattr(FS, "INCREMENTAL_ABOVE_BYTES", 100)
    w = LiveImage()
    reg, _ = _camera_registry()
    ds = run(_recipe(n=4), reg, created_iso="t", data_path=tmp_path / "big.nc")
    assert "cam.image" not in ds.data_vars
    w.set_dataset(ds)
    assert not w.isHidden() and "point [3]" in w.caption.text()
