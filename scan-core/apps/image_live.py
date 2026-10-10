"""image_live.py -- the newest camera frame of a running scan (2026-10-10).

When a scan records an IMAGE detector (a camera frame per point,
scan_core/framestore.py) the map next to it shows one number per point; what
the camera actually saw at the point just measured is this small picture.

Where the frame comes from, in order:
  1. the engine's newest frame (framestore.latest_frames(ds)) -- also for a
     big map whose frames are written straight into the file and are not in
     the dataset at all;
  2. otherwise (a dataset from a scan server's mirror, or a saved file) the
     last point whose per-point mask `<det>_measured` says a frame was taken,
     in the order the odometer measures (C order).

Hidden while no image detector is recorded. Colours: grey levels from the
frame's own 0.5 / 99.5 percentiles, so a dim frame is still visible; the
caption gives the point, the range and the bit depth.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from scan_core import framestore as FS


def image_variables(ds) -> list[str]:
    """Image detectors of `ds`: in the dataset (attr aaltoflow_image) or
    written to the file as they came (frames_of / latest_frames)."""
    names = []
    if ds is None:
        return names
    for name, da in ds.data_vars.items():
        if da.attrs.get(FS.IMAGE_ATTR):
            names.append(str(name))
    for name in list(FS.latest_frames(ds)) + list(FS.frames_of(ds)):
        if name not in names:
            names.append(name)
    return names


def newest_frame(ds, det: str):
    """(index tuple, 2-D frame as float) of the newest frame of `det`, or None."""
    latest = FS.latest_frames(ds).get(det)
    if latest is not None:
        idx, frame = latest
        return tuple(idx), np.asarray(frame, dtype=float)
    if det not in ds.data_vars:
        return None
    da = ds[det]
    scan_dims = [d for d in da.dims][:-2]
    mask_name = FS.mask_name(det)
    if mask_name in ds.data_vars:
        mask = np.asarray(ds[mask_name].values, dtype=bool)
    else:
        # no mask (an older file): a frame with any value counts as taken
        mask = np.isfinite(np.asarray(da.values, dtype=float)).any(axis=(-2, -1))
    if not np.any(mask):
        return None
    flat = int(np.flatnonzero(np.ravel(mask))[-1])
    idx = np.unravel_index(flat, np.shape(mask)) if scan_dims else ()
    frame = np.asarray(da.values[tuple(idx)], dtype=float)
    return tuple(int(i) for i in idx), frame


class LiveImage(QtWidgets.QFrame):
    """A small picture of the newest frame, with a caption."""

    def __init__(self, width: int = 280):
        super().__init__()
        self.setObjectName("card")
        self.setFixedWidth(width)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(4)
        tag = QtWidgets.QLabel("CAMERA · newest frame")
        tag.setObjectName("tag")
        v.addWidget(tag)
        self.combo = QtWidgets.QComboBox()
        self.combo.setToolTip("Which image detector (when the scan records several).")
        self.combo.currentIndexChanged.connect(lambda *_: self._draw())
        v.addWidget(self.combo)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setMinimumHeight(200)
        self.vb = self.glw.addViewBox(lockAspect=True, enableMouse=False)
        # image row 0 at the TOP, as the camera's own window shows it
        self.vb.invertY(True)
        self.img = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.img)
        v.addWidget(self.glw, 1)
        self.caption = QtWidgets.QLabel("")
        self.caption.setObjectName("hint")
        self.caption.setWordWrap(True)
        v.addWidget(self.caption)
        self.ds = None
        self.setVisible(False)

    def set_dataset(self, ds) -> None:
        """Called with every live snapshot (and the final dataset)."""
        self.ds = ds
        names = image_variables(ds)
        self.setVisible(bool(names))
        if not names:
            return
        if [self.combo.itemText(i) for i in range(self.combo.count())] != names:
            keep = self.combo.currentText()
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.addItems(names)
            if keep in names:
                self.combo.setCurrentText(keep)
            self.combo.blockSignals(False)
        self.combo.setVisible(len(names) > 1)
        self._draw()

    def _draw(self) -> None:
        det = self.combo.currentText()
        if self.ds is None or not det:
            return
        got = newest_frame(self.ds, det)
        if got is None:
            self.img.clear()
            self.caption.setText("no frame yet")
            return
        idx, frame = got
        ok = np.isfinite(frame)
        if not ok.any():
            self.img.clear()
            self.caption.setText(f"point {list(idx)}: empty frame")
            return
        lo, hi = np.percentile(frame[ok], [0.5, 99.5])
        if hi <= lo:
            hi = lo + 1.0
        self.img.setImage(np.where(ok, frame, lo), levels=(lo, hi), autoLevels=False)
        self.vb.autoRange(padding=0.0)
        bits = ""
        if det in self.ds.data_vars and self.ds[det].attrs.get("declared_bits"):
            bits = f" · {int(self.ds[det].attrs['declared_bits'])} bit"
        h, w = frame.shape[:2]
        self.caption.setText(f"point {list(idx)} · {w} x {h} px · "
                             f"{np.nanmin(frame):.0f} .. {np.nanmax(frame):.0f}{bits}")

    def sizeHint(self) -> QtCore.QSize:          # noqa: N802 (Qt name)
        return QtCore.QSize(self.width(), 300)
