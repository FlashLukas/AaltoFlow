"""The Spot tab: define the laser spot by thresholding, then CALIBRATE its position.

How the spot is used (decided with Lukas, 2026-09-13):

  * POSITION is a user decision. The laser spot is fixed in the image, so you
    set the threshold here, press "Calibrate spot", and that averaged centroid
    is THE spot position -- the one click-to-go and the stabiliser use -- until
    you calibrate again. It is not re-derived from every frame.
  * SIZE is evaluated every frame by the brain, thresholding only a small search
    box around the calibrated position ("search region"). This tab shows that
    live size and whether the live centroid has wandered from the calibration.

SIZE WITHOUT A FIXED THRESHOLD (2026-09-28): a defocused coherent spot has
rings and a central hole, which a fixed threshold cuts wrongly. The brain also
measures, every frame, the second moment (D4sigma, ISO 11146) and the area
above a fraction of the spot's own peak; card 4 picks which of the three sizes
is "the" size (the trace plots it) and tunes both. On the grabbed frame the
zoom draws the D4sigma ellipse and its integration box.

So the thresholding here works on a GRABBED frame, not the live stream: grab
one, move the sliders (the tint, zoom, histogram and detection readout redraw
on that snapshot), and calibrate when exactly the spot is selected. The input
widgets are filled from the config once and never rewritten by a refresh (the Set Z
button once sent the CURRENT Z because a timer rewrote its box: never write
live status into an input the user types into).
"""

from __future__ import annotations

import time
from collections import deque

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import theme as T
from .camera_view import SPOT_GREEN, SPOT_TINT_BGRA, CameraView, outlined_pen
from .plots import MiniPlot
from .. import vision as V
from ..config import CLIP_MODES, SIZE_METHODS

# What each size method is called in the combo, and the unit of its number.
SIZE_LABELS = {"threshold": "fixed threshold (area)",
               "relative": "relative to the peak (area)",
               "d4sigma": "second moment D4sigma (diameter)"}
SIZE_UNITS = {"threshold": "px²", "relative": "px²", "d4sigma": "px"}

ZOOM_HALF = 48          # px of frame around the spot shown in the zoom (96x96)
AREA_WINDOW_S = 30.0    # the spot-area trace shows this many seconds
AREA_SAMPLES = 1000     # ring buffer: > 30 s at the 15-20 fps the camera runs


def spot_mask(gray: np.ndarray, lo: int, hi: int, bright: bool) -> np.ndarray:
    """The same pixel selection vision.find_spot uses (before blob selection)."""
    if not bright:
        gray = cv2.bitwise_not(gray)
        lo, hi = 255 - hi, 255 - lo
    return cv2.inRange(gray, int(lo), int(hi))


def analyse(gray: np.ndarray, spot_cfg) -> dict:
    """What the current threshold selects on this frame (whole-frame search, as
    calibration does)."""
    sp = spot_cfg
    mask = spot_mask(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot)
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
    h, w = gray.shape[:2]
    if n > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        big = (areas >= max(1, sp.min_area_px))
        cand = V.spot_candidates(stats, (0, 0), (w, h), sp.min_area_px, sp.max_area_px,
                                 sp.reject_border)
        too_large = big & (areas > sp.max_area_px) if sp.max_area_px > 0 else big & False
        at_edge = big & ~cand & ~too_large
    else:
        areas = np.array([], int)
        big = cand = too_large = at_edge = np.array([], bool)
    rep = V.find_spot(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot, 0, None, sp.min_area_px,
                      sp.max_area_px, sp.reject_border)
    out = {"mask": mask, "spot": rep, "selected_px": int(np.count_nonzero(mask)),
           "blobs": int(np.count_nonzero(cand)),            # candidates for the spot
           "too_large": int(np.count_nonzero(too_large)),   # rejected: area > max
           "at_edge": int(np.count_nonzero(at_edge)),       # rejected: touches the frame edge
           "blobs_any": int(len(areas)), "frame_max": int(gray.max()),
           "saturated_px": 0, "peak": 0}
    if rep.found:
        x, y, w, h = rep.bbox
        roi = gray[y:y + h, x:x + w]
        if roi.size:
            out["peak"] = int(roi.max())
            out["saturated_px"] = int(np.count_nonzero(roi >= 255 if sp.bright_spot else roi <= 0))
    return out


def suggest_threshold(gray: np.ndarray, bright: bool) -> tuple[int, str]:
    """Halfway between the background's bright tail (99.5th percentile) and the
    brightest pixel; refuses when there is no distinct spot to separate."""
    g = gray if bright else cv2.bitwise_not(gray)
    tail, peak = float(np.percentile(g, 99.5)), float(g.max())
    if peak - tail < 20:
        return -1, (f"no distinct {'bright' if bright else 'dark'} spot: brightest pixel "
                    f"{peak:.0f} vs background tail {tail:.0f}")
    thr = int(round(tail + 0.5 * (peak - tail)))
    return thr, f"background tail {tail:.0f}, peak {peak:.0f} -> lower threshold {thr}"


class SpotZoom(QWidget):
    """Pixel-exact enlargement: tint, detected centroid (+), calibrated position ([])."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self._img = self._mask_img = None
        self._buf = self._mask_buf = None
        self._origin, self._size = (0, 0), (1, 1)
        self._live = self._calib = None
        self._moments = None                   # vision.SpotMoments of this snapshot

    def set_moments(self, m):
        """The second moment of the snapshot: drawn as the D4sigma ellipse
        (its axes are the principal axes of the intensity) and the box it was
        integrated over. None = nothing to draw."""
        self._moments = m if (m is not None and m.ok) else None
        self.update()

    def set_data(self, gray, mask, center, live, calib):
        h, w = gray.shape
        cx, cy = int(round(center[0])), int(round(center[1]))
        x0, y0 = max(0, cx - ZOOM_HALF), max(0, cy - ZOOM_HALF)
        x1, y1 = min(w, cx + ZOOM_HALF), min(h, cy + ZOOM_HALF)
        if x1 <= x0 or y1 <= y0:
            return
        self._buf = np.ascontiguousarray(gray[y0:y1, x0:x1])
        argb = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
        argb[mask[y0:y1, x0:x1] > 0] = SPOT_TINT_BGRA
        self._mask_buf = np.ascontiguousarray(argb)
        self._img = QImage(self._buf.data, x1 - x0, y1 - y0, x1 - x0, QImage.Format_Grayscale8)
        self._mask_img = QImage(self._mask_buf.data, x1 - x0, y1 - y0, 4 * (x1 - x0),
                                QImage.Format_ARGB32)
        self._origin, self._size = (x0, y0), (x1 - x0, y1 - y0)
        self._live, self._calib = live, calib
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.COLORS["code_bg"]))
        if self._img is None:
            p.setPen(QColor(T.COLORS["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "grab a frame")
            p.end()
            return
        sw, sh = self._size
        scale = min(self.width() / sw, self.height() / sh)
        rect = QRectF((self.width() - sw * scale) / 2, (self.height() - sh * scale) / 2,
                      sw * scale, sh * scale)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)   # show real pixels
        p.drawImage(rect, self._img)
        p.drawImage(rect, self._mask_img)
        p.setRenderHint(QPainter.Antialiasing, True)

        def to_w(x, y):   # a centroid at pixel index i is the middle of that pixel
            return QPointF(rect.left() + (x - self._origin[0] + 0.5) * scale,
                           rect.top() + (y - self._origin[1] + 0.5) * scale)

        p.setBrush(Qt.NoBrush)
        if self._live is not None:                 # detected centroid: dashed ring
            c = to_w(*self._live)
            for _ in outlined_pen(p, SPOT_GREEN, 1.5, Qt.DashLine):
                p.drawEllipse(c, 9, 9)
        if self._calib is not None:                # THE position: gapped crosshair
            c = to_w(*self._calib)
            g = 16
            for _ in outlined_pen(p, SPOT_GREEN, 2.0):
                p.drawLine(QPointF(c.x() - g, c.y()), QPointF(c.x() - 5, c.y()))
                p.drawLine(QPointF(c.x() + 5, c.y()), QPointF(c.x() + g, c.y()))
                p.drawLine(QPointF(c.x(), c.y() - g), QPointF(c.x(), c.y() - 5))
                p.drawLine(QPointF(c.x(), c.y() + 5), QPointF(c.x(), c.y() + g))
        m = self._moments
        if m is not None:
            # D4sigma ellipse: the eigenvectors of the 2x2 second-moment
            # matrix are its axes, 2 sqrt(eigenvalue) x 2 its semi-axes (the
            # D4sigma diameter is 4 sigma along each principal axis)
            cov = np.array([[m.sigma2_x, m.sigma2_xy], [m.sigma2_xy, m.sigma2_y]])
            ev, vec = np.linalg.eigh(cov)
            ev = np.clip(ev, 0.0, None)
            ang = float(np.degrees(np.arctan2(vec[1, 1], vec[0, 1])))
            c = to_w(m.cx, m.cy)
            p.save()
            p.translate(c)
            p.rotate(ang)
            for _ in outlined_pen(p, T.COLORS["accent"], 1.5):
                p.drawEllipse(QPointF(0, 0), 2.0 * np.sqrt(ev[1]) * scale,
                              2.0 * np.sqrt(ev[0]) * scale)
            p.restore()
            x0, y0, x1, y1 = m.box
            a, b = to_w(x0 - 0.5, y0 - 0.5), to_w(x1 - 0.5, y1 - 0.5)
            for _ in outlined_pen(p, T.COLORS["accent"], 1.0, Qt.DotLine):
                p.drawRect(QRectF(a, b))
        p.setPen(QColor(T.COLORS["muted"]))
        txt = f"{sw}x{sh} px · cross: position · ring: detected"
        if m is not None:                       # short: the zoom is narrow
            txt = f"{sw}x{sh} · +: position · ring: detected · amber: D4σ"
        p.drawText(6, self.height() - 6, txt)
        p.end()


def _card(title: str):
    f = QFrame(); f.setObjectName("card")
    lay = QVBoxLayout(f); lay.setContentsMargins(10, 8, 10, 10); lay.setSpacing(6)
    lab = QLabel(title.upper()); lab.setObjectName("cardTitle")
    lay.addWidget(lab)
    return f, lay


class SpotTab(QWidget):
    def __init__(self, ctrl, cfg, log, frame_source, parent=None):
        super().__init__(parent)
        self.ctrl, self.cfg, self.log = ctrl, cfg, log
        self._frame_source = frame_source      # callable -> latest processed frame
        self._gray = None                      # the grabbed snapshot
        self._status = None
        # spot-area trace: (monotonic time, area or 0 when not seen), one per NEW frame
        self._area = deque(maxlen=AREA_SAMPLES)
        self._area_last_frame = None
        self._pending = QTimer(self)           # push threshold edits after a pause
        self._pending.setSingleShot(True)
        self._pending.setInterval(300)
        self._pending.timeout.connect(self._push_threshold)
        self._build()

    # -- layout -------------------------------------------------------------------
    def _build(self):
        root = QHBoxLayout(self)
        left = QVBoxLayout(); root.addLayout(left, 3)
        self.view = CameraView()
        self.view.set_show_threshold(True)
        left.addWidget(self.view, 3)
        row = QHBoxLayout()
        self.zoom = SpotZoom()
        row.addWidget(self.zoom, 1)
        # spot AREA over time, next to the zoom: the per-frame size check
        area_box = QVBoxLayout()
        self.area_plot = MiniPlot(xlabel="seconds ago", ylabel="spot area px²")
        area_box.addWidget(self.area_plot, 1)
        ar = QHBoxLayout()
        self.lab_area = QLabel("-"); self.lab_area.setObjectName("muted")
        ar.addWidget(self.lab_area, 1)
        self.chk_area_pause = QCheckBox("pause")
        ar.addWidget(self.chk_area_pause)
        b = QPushButton("Clear"); b.clicked.connect(self._clear_area)
        ar.addWidget(b)
        area_box.addLayout(ar)
        row.addLayout(area_box, 2)
        left.addLayout(row, 2)
        self.hist = MiniPlot(xlabel="intensity (0-255)", ylabel="pixels per level (log10)")
        self.hist.setMinimumHeight(110)
        left.addWidget(self.hist, 1)

        right = QVBoxLayout(); root.addLayout(right, 2)
        sp = self.cfg.spot

        f, l = _card("1 · Grab a frame")
        r = QHBoxLayout()
        b = QPushButton("Grab frame"); b.clicked.connect(self.grab_frame)
        r.addWidget(b)
        self.lab_snap = QLabel("no frame grabbed"); self.lab_snap.setObjectName("muted")
        r.addWidget(self.lab_snap, 1)
        l.addLayout(r)
        right.addWidget(f)

        f, l = _card("2 · Threshold  (which pixels are the spot)")
        self.cmb_kind = QComboBox(); self.cmb_kind.addItems(["bright spot", "dark spot"])
        self.cmb_kind.setCurrentIndex(0 if sp.bright_spot else 1)
        self.cmb_kind.currentIndexChanged.connect(self._on_edit)
        l.addWidget(self.cmb_kind)
        grid = QGridLayout()
        self.sl_lo, self.sp_lo = self._slider_pair(grid, 0, "lower", int(sp.thr_lower))
        self.sl_hi, self.sp_hi = self._slider_pair(grid, 1, "upper", int(sp.thr_upper))
        l.addLayout(grid)
        form = QFormLayout()
        self.sp_area = QSpinBox(); self.sp_area.setRange(1, 1_000_000)
        self.sp_area.setValue(int(sp.min_area_px)); self.sp_area.valueChanged.connect(self._on_edit)
        form.addRow("min area (px²)", self.sp_area)
        self.sp_maxarea = QSpinBox(); self.sp_maxarea.setRange(0, 10_000_000)
        self.sp_maxarea.setValue(int(sp.max_area_px)); self.sp_maxarea.valueChanged.connect(self._on_edit)
        self.sp_maxarea.setToolTip("Blobs larger than this are not the spot (e.g. a saturated "
                                   "illumination patch). 0 = no limit.")
        form.addRow("max area (px²)", self.sp_maxarea)
        self.chk_edge = QCheckBox("ignore blobs touching the frame edge")
        self.chk_edge.setChecked(bool(sp.reject_border)); self.chk_edge.toggled.connect(self._on_edit)
        form.addRow("", self.chk_edge)
        self.chk_sym = QCheckBox("per frame: only what is symmetric about the spot centre")
        self.chk_sym.setToolTip("An object reaching into the search region is then neither "
                                "taken for the spot nor added to its area.")
        self.chk_sym.setChecked(bool(sp.symmetric)); self.chk_sym.toggled.connect(self._on_edit)
        form.addRow("", self.chk_sym)
        l.addLayout(form)

        # Search region: where the per-frame size check looks, around the
        # calibrated position (calibration itself searches the whole frame).
        form = QFormLayout()
        self.cmb_shape = QComboBox(); self.cmb_shape.addItems(["rectangle", "circle"])
        self.cmb_shape.setCurrentIndex(1 if sp.search_shape == "circle" else 0)
        self.cmb_shape.currentIndexChanged.connect(self._on_shape)
        form.addRow("search region", self.cmb_shape)
        self.sp_look = QSpinBox(); self.sp_look.setRange(0, 5000)
        self.sp_look.setValue(int(sp.lookup_region_px)); self.sp_look.valueChanged.connect(self._on_edit)
        self.sp_look.setToolTip("0 = search the whole frame every time")
        self.lab_look = QLabel("half-width ± x (px)")
        form.addRow(self.lab_look, self.sp_look)
        self.sp_look_y = QSpinBox(); self.sp_look_y.setRange(0, 5000)
        self.sp_look_y.setValue(int(sp.lookup_region_y_px)); self.sp_look_y.valueChanged.connect(self._on_edit)
        self.sp_look_y.setToolTip("0 = same as the half-width (a square)")
        self.lab_look_y = QLabel("half-height ± y (px)")
        form.addRow(self.lab_look_y, self.sp_look_y)
        l.addLayout(form)
        self._on_shape(update=False)
        b = QPushButton("Suggest threshold from the grabbed frame")
        b.clicked.connect(self._suggest)
        l.addWidget(b)
        self.lab_det = QLabel("-"); self.lab_det.setWordWrap(True)
        self.lab_det.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.lab_det)
        self.lab_warn = QLabel(""); self.lab_warn.setWordWrap(True)
        self.lab_warn.setStyleSheet(f"color:{T.COLORS['accent']};")
        l.addWidget(self.lab_warn)
        right.addWidget(f)

        f, l = _card("3 · Spot position  (calibrate from the threshold, or enter it)")
        r = QHBoxLayout()
        r.addWidget(QLabel("average over"))
        self.sp_frames = QSpinBox(); self.sp_frames.setRange(2, 30); self.sp_frames.setValue(20)
        r.addWidget(self.sp_frames); r.addWidget(QLabel("frames"))
        self.b_cal = QPushButton("Calibrate spot"); self.b_cal.setObjectName("primary")
        self.b_cal.clicked.connect(self._calibrate)
        r.addWidget(self.b_cal, 1)
        l.addLayout(r)

        # Manual entry. The boxes are a TARGET: filled once from the config, by
        # a click on the image (if picking), or after a calibration -- never by
        # the refresh -- and nothing changes in the brain until "Set position".
        r = QHBoxLayout()
        r.addWidget(QLabel("x"))
        self.sp_x = QDoubleSpinBox(); self.sp_x.setRange(0, 100000); self.sp_x.setDecimals(2)
        r.addWidget(self.sp_x)
        r.addWidget(QLabel("y"))
        self.sp_y = QDoubleSpinBox(); self.sp_y.setRange(0, 100000); self.sp_y.setDecimals(2)
        r.addWidget(self.sp_y)
        r.addWidget(QLabel("px"))
        b = QPushButton("Set position"); b.clicked.connect(self._set_manual)
        r.addWidget(b)
        b = QPushButton("Clear"); b.setObjectName("danger"); b.clicked.connect(self._clear_position)
        r.addWidget(b)
        l.addLayout(r)
        self.chk_pick = QCheckBox("Pick x/y by clicking the image (then press Set position)")
        l.addWidget(self.chk_pick)
        self.view.clicked.connect(self._on_pick)
        if sp.ref_set:
            self.sp_x.setValue(float(sp.ref_x)); self.sp_y.setValue(float(sp.ref_y))

        self.lab_ref = QLabel("-"); self.lab_ref.setWordWrap(True)
        l.addWidget(self.lab_ref)
        b = QPushButton("Save camera settings to file (survives restart)")
        b.clicked.connect(self._save)
        l.addWidget(b)
        right.addWidget(f)

        f, l = _card("4 · Spot size  (without a fixed threshold)")
        note = QLabel("A defocused laser spot has rings and a hole, which a fixed threshold "
                      "cuts wrongly. All three sizes are measured every frame; the chosen one "
                      "is plotted. The autofocus picks its own (AutoFocus tab: spot_area / "
                      "spot_relative / spot_d4sigma).")
        note.setWordWrap(True); note.setObjectName("muted")
        l.addWidget(note)
        rows = []                      # (label, widget), laid out two per row below
        self.cmb_size = QComboBox()
        for key in SIZE_METHODS:
            self.cmb_size.addItem(SIZE_LABELS.get(key, key), key)
        self.cmb_size.setCurrentIndex(max(0, list(SIZE_METHODS).index(sp.size_method)
                                          if sp.size_method in SIZE_METHODS else 0))
        self.cmb_size.currentIndexChanged.connect(self._on_edit)
        rows.append(("size shown", self.cmb_size))
        self.sp_rel = self._dspin(sp.rel_level, 0.01, 0.99, 0.005, 3,
                                  "relative: fraction of the peak above background "
                                  "(0.135 = 1/e², 0.5 = FWHM)")
        rows.append(("relative level", self.sp_rel))
        self.sp_clip = self._dspin(sp.clip_sigma, 0.5, 10.0, 0.5, 1,
                                   "D4sigma: a pixel counts when (blurred) above this many "
                                   "noise sigmas of the background")
        rows.append(("clip (noise σ)", self.sp_clip))
        self.cmb_clipmode = QComboBox()
        for key in CLIP_MODES:
            self.cmb_clipmode.addItem({"local": "local (generous)",
                                       "pixel": "pixel (textbook)"}.get(key, key), key)
        self.cmb_clipmode.setToolTip("local: decided on a blurred copy, generous (default). "
                                     "pixel: the textbook per-pixel clip -- deletes faint "
                                     "wings, sigma^2 too small out of focus.")
        self.cmb_clipmode.setCurrentIndex(max(0, list(CLIP_MODES).index(sp.clip_mode)
                                              if sp.clip_mode in CLIP_MODES else 0))
        self.cmb_clipmode.currentIndexChanged.connect(self._on_edit)
        rows.append(("which pixels", self.cmb_clipmode))
        self.sp_detect = self._dspin(sp.detect_px, 0.0, 10.0, 0.5, 1,
                                     "D4sigma: blur (px) of the copy on which 'significant' is "
                                     "decided; the values themselves stay raw")
        rows.append(("decision blur (px)", self.sp_detect))
        self.sp_blob = QSpinBox(); self.sp_blob.setRange(0, 10000)
        self.sp_blob.setValue(int(sp.min_blob_px)); self.sp_blob.valueChanged.connect(self._on_edit)
        self.sp_blob.setToolTip("D4sigma: smaller clumps of 'significant' pixels are noise")
        rows.append(("min clump (px)", self.sp_blob))
        self.sp_grow = QSpinBox(); self.sp_grow.setRange(0, 50)
        self.sp_grow.setValue(int(sp.mask_grow_px)); self.sp_grow.valueChanged.connect(self._on_edit)
        self.sp_grow.setToolTip("D4sigma: widen the kept region by this many px, so the faint "
                                "fringe of each wing is counted")
        rows.append(("grow mask (px)", self.sp_grow))
        self.sp_boxf = self._dspin(sp.box_factor, 0.5, 5.0, 0.1, 2,
                                   "D4sigma: box half-size in D4sigma (1.5 = 3 x D4sigma wide, ISO)")
        rows.append(("box factor", self.sp_boxf))
        self.sp_iter = QSpinBox(); self.sp_iter.setRange(1, 50)
        self.sp_iter.setValue(int(sp.max_iter)); self.sp_iter.valueChanged.connect(self._on_edit)
        rows.append(("max iterations", self.sp_iter))
        self.sp_smooth = self._dspin(sp.smooth_px, 0.0, 5.0, 0.5, 1,
                                     "blur the values first (px); its own width is subtracted "
                                     "again, so the focus cannot move")
        rows.append(("smooth values (px)", self.sp_smooth))
        self.chk_asym = QCheckBox("ignore light with no twin opposite the spot centre")
        self.chk_asym.setToolTip("A feature of the sample next to the spot has no mirror image "
                                 "through the calibrated centre; the spot always has one.")
        self.chk_asym.setChecked(bool(sp.reject_asymmetric))
        self.chk_asym.toggled.connect(self._on_edit)
        rows.append(("", self.chk_asym))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        # the method first, on a row of its own; the tuning knobs two per row
        grid.addWidget(QLabel(rows[0][0]), 0, 0)
        grid.addWidget(rows[0][1], 0, 1, 1, 3)
        r, c = 1, 0
        for label, widget in rows[1:]:
            if not label:                     # a checkbox: a whole row of its own
                r, c = (r + 1, 0) if c else (r, 0)
                grid.addWidget(widget, r, 0, 1, 4)
                r += 1
                continue
            grid.addWidget(QLabel(label), r, c)
            grid.addWidget(widget, r, c + 1)
            r, c = (r, 2) if c == 0 else (r + 1, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        l.addLayout(grid)
        self.lab_size = QLabel("-"); self.lab_size.setWordWrap(True)
        self.lab_size.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.lab_size)
        right.addWidget(f)

        f, l = _card("Live  (the brain's per-frame size check)")
        self.lab_live = QLabel("-"); self.lab_live.setWordWrap(True)
        l.addWidget(self.lab_live)
        right.addWidget(f)
        right.addStretch(1)
        self._show_ref()

    def _dspin(self, value, lo, hi, step, decimals, tip):
        w = QDoubleSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
        w.setDecimals(decimals); w.setValue(float(value)); w.setToolTip(tip)
        w.valueChanged.connect(self._on_edit)
        return w

    def _slider_pair(self, grid, row, label, value):
        sl = QSlider(Qt.Horizontal); sl.setRange(0, 255); sl.setValue(value)
        sp = QSpinBox(); sp.setRange(0, 255); sp.setValue(value)
        sl.valueChanged.connect(sp.setValue)
        sp.valueChanged.connect(sl.setValue)
        sp.valueChanged.connect(self._on_edit)
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(sl, row, 1)
        grid.addWidget(sp, row, 2)
        return sl, sp

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._gray is None:
            self.grab_frame()           # something to threshold when the tab opens

    # -- snapshot + threshold --------------------------------------------------------
    def grab_frame(self):
        try:
            gray = self._frame_source()
        except Exception as exc:
            self.log("warn", f"grab frame: {exc}")
            return
        if gray is None:
            self.lab_snap.setText("no frame from the camera yet")
            return
        if gray.ndim == 3:
            gray = gray[..., 0]
        self._gray = np.ascontiguousarray(gray)
        self.lab_snap.setText(f"{gray.shape[1]}x{gray.shape[0]} grabbed")
        self._reanalyse()

    def values(self) -> dict:
        lo, hi = self.sp_lo.value(), self.sp_hi.value()
        return {"thr_lower": min(lo, hi), "thr_upper": max(lo, hi),
                "bright_spot": self.cmb_kind.currentIndex() == 0,
                "min_area_px": self.sp_area.value(),
                "max_area_px": self.sp_maxarea.value(),
                "reject_border": self.chk_edge.isChecked(),
                "symmetric": self.chk_sym.isChecked(),
                "search_shape": "circle" if self.cmb_shape.currentIndex() == 1 else "rect",
                "lookup_region_px": self.sp_look.value(),
                "lookup_region_y_px": self.sp_look_y.value(),
                # threshold-free sizes (card 4)
                "size_method": self.cmb_size.currentData() or "threshold",
                "rel_level": self.sp_rel.value(),
                "clip_sigma": self.sp_clip.value(),
                "clip_mode": self.cmb_clipmode.currentData() or "local",
                "detect_px": self.sp_detect.value(),
                "min_blob_px": self.sp_blob.value(),
                "mask_grow_px": self.sp_grow.value(),
                "box_factor": self.sp_boxf.value(),
                "max_iter": self.sp_iter.value(),
                "smooth_px": self.sp_smooth.value(),
                "reject_asymmetric": self.chk_asym.isChecked()}

    def _on_shape(self, *_, update=True):
        circle = self.cmb_shape.currentIndex() == 1
        self.lab_look.setText("radius (px)" if circle else "half-width ± x (px)")
        self.lab_look_y.setVisible(not circle)
        self.sp_look_y.setVisible(not circle)
        if update:
            self._on_edit()

    def _on_edit(self, *_):
        for k, v in self.values().items():        # local mirror: the tint follows now
            setattr(self.cfg.spot, k, v)
        self._reanalyse()
        self._pending.start()                      # the brain, after a short pause

    def _push_threshold(self):
        try:
            self.ctrl.set_config({"spot": self.values()})
        except Exception as exc:
            self.log("error", f"spot threshold not applied: {exc}")

    def _reanalyse(self):
        gray = self._gray
        if gray is None:
            return
        sp = self.cfg.spot
        a = analyse(gray, sp)
        rep = a["spot"]
        calib = (sp.ref_x, sp.ref_y) if sp.ref_set else None
        live = (rep.cx, rep.cy) if rep.found else None
        center = live or calib or (gray.shape[1] / 2, gray.shape[0] / 2)
        self.view.set_frame(gray)
        self.view.set_overlay(self._status, self.cfg)
        self.zoom.set_data(gray, a["mask"], center, live, calib)
        # the threshold-free sizes of this snapshot, around the calibrated
        # position (else what the threshold found) -- as the brain does
        guess = calib or live
        mom = V.spot_second_moment(gray, guess, sp) if guess is not None else None
        rel = V.spot_relative_area(gray, guess, sp) if guess is not None else None
        self.zoom.set_moments(mom)
        self.lab_size.setText(self._size_text(mom, rel))

        if rep.found:
            x, y, w, h = rep.bbox
            txt = (f"<b>spot found</b> at ({rep.cx:.2f}, {rep.cy:.2f}) px<br>"
                   f"area {rep.area:.0f} px² · box {w}×{h} px · axes {rep.major:.1f}/{rep.minor:.1f} px"
                   f" · {rep.orientation_deg:+.0f}°<br>"
                   f"candidates: {a['blobs']} (largest used) · spot peak {a['peak']}")
            if calib is not None:
                txt += (f"<br>vs calibrated position: "
                        f"{np.hypot(rep.cx - sp.ref_x, rep.cy - sp.ref_y):.2f} px")
        else:
            txt = (f"<b>no spot</b>: {a['selected_px']} pixels selected in {a['blobs_any']} "
                   f"blobs · frame max {a['frame_max']}")
        if a["too_large"] or a["at_edge"]:
            txt += (f"<br>ignored: {a['too_large']} larger than max area, "
                    f"{a['at_edge']} touching the frame edge")
        self.lab_det.setText(txt)

        warn = []
        if a["saturated_px"]:
            warn.append(f"{a['saturated_px']} saturated pixels in the spot: its centre of mass "
                        f"is biased -- lower the exposure or gain before calibrating")
        if rep.found and a["blobs"] > 1:
            warn.append(f"{a['blobs']} candidate blobs; only the largest is used -- check it is "
                        f"the laser (zoom), or raise the lower threshold / min area")
        if a["selected_px"] > 0.05 * gray.size:
            warn.append("more than 5 % of the frame is selected: the threshold catches background")
        self.lab_warn.setText("\n".join(warn))

        counts = np.bincount(gray.ravel(), minlength=256).astype(float)
        logc = np.log10(counts + 1.0)
        top = float(logc.max()) or 1.0
        self.hist.set_series([
            (list(range(256)), logc.tolist(), T.COLORS["text"], "pixels"),
            ([sp.thr_lower, sp.thr_lower], [0.0, top * 1.05], SPOT_GREEN, "lower"),
            ([sp.thr_upper, sp.thr_upper], [0.0, top * 1.05], SPOT_GREEN, "upper"),
        ], x_range=(0, 255), y_min=0.0, y_max=top * 1.08)

    def _size_text(self, mom, rel) -> str:
        """The threshold-free sizes of the grabbed frame, in words."""
        if mom is None:
            return "no spot position yet: calibrate (or find the spot with the threshold)"
        parts = []
        if mom.ok:
            parts.append(f"<b>D4σ {mom.d4sigma:.1f} px</b> (x {mom.d4sigma_x:.1f}, y "
                         f"{mom.d4sigma_y:.1f}) · σ² {mom.sigma2:.1f} px² · centroid "
                         f"({mom.cx:.1f}, {mom.cy:.1f}) · {mom.n_iter} iteration(s)"
                         + ("" if mom.converged else " NOT converged")
                         + (" · box hit the search region: enlarge it" if mom.clipped else ""))
            parts.append(f"background {mom.background:.1f} ± {mom.noise:.2f} · peak "
                         f"{mom.peak:.0f} above it")
        else:
            parts.append(f"D4σ: {mom.why}")
        if rel is not None:
            parts.append(f"relative area {rel.area:.0f} px² above {rel.level:.1f}"
                         if rel.ok else f"relative area: {rel.why}")
        if mom.saturated or (rel is not None and rel.saturated):
            parts.append("<span style='color:%s'>SATURATED: every size is wrong (σ² too "
                         "big) -- lower the exposure</span>" % T.COLORS["danger"])
        return "<br>".join(parts)

    def _suggest(self):
        if self._gray is None:
            self.log("warn", "grab a frame first")
            return
        thr, why = suggest_threshold(self._gray, self.cmb_kind.currentIndex() == 0)
        if thr < 0:
            self.log("warn", why)
            return
        self.sp_hi.setValue(255)
        self.sp_lo.setValue(thr)
        self.log("info", f"suggested threshold: {why}")

    # -- calibration ----------------------------------------------------------------
    def _calibrate(self):
        self._pending.stop()
        self._push_threshold()                     # calibrate with what is on screen
        try:
            res = self.ctrl.calibrate_spot(self.sp_frames.value())
        except Exception as exc:
            self.log("error", f"calibrate spot: {exc}")
            return
        self._adopt(res)
        self.grab_frame()
        self.log("info", f"spot calibrated at ({res['x']:.2f}, {res['y']:.2f}) px ± "
                         f"{res['jitter_px']:.2f} px over {res['frames']} frames -- now used by "
                         f"click-to-go and the stabiliser; 'Save' keeps it after a restart")

    def _adopt(self, res: dict):
        """Mirror a position the brain accepted, and show it in the entry boxes."""
        sp = self.cfg.spot
        sp.ref_x, sp.ref_y, sp.ref_area = res["x"], res["y"], res["area"]
        sp.ref_jitter_px, sp.ref_set = res["jitter_px"], True
        sp.ref_d4sigma_px = float(res.get("d4sigma_px", 0.0) or 0.0)
        sp.ref_rel_area = float(res.get("rel_area", 0.0) or 0.0)
        self.sp_x.setValue(float(res["x"])); self.sp_y.setValue(float(res["y"]))
        self._show_ref()
        self._reanalyse()

    def _on_pick(self, x, y):
        if self.chk_pick.isChecked():
            self.sp_x.setValue(float(x)); self.sp_y.setValue(float(y))

    def _set_manual(self):
        try:
            res = self.ctrl.set_spot_position(self.sp_x.value(), self.sp_y.value())
        except Exception as exc:
            self.log("error", f"set spot position: {exc}")
            return
        self._adopt(res)
        self.chk_pick.setChecked(False)
        self.log("info", f"spot position set to ({res['x']:.2f}, {res['y']:.2f}) px -- used by "
                         f"click-to-go and the stabiliser; 'Save' keeps it after a restart")

    def _clear_position(self):
        try:
            self.ctrl.clear_spot_position()
        except Exception as exc:
            self.log("error", f"clear spot position: {exc}")
            return
        self.cfg.spot.ref_set = False
        self._show_ref()
        self._reanalyse()

    def _save(self):
        try:
            self._pending.stop()
            self._push_threshold()
            path = self.ctrl.save_config()
            self.log("info", f"camera settings saved to {path} (loaded at service start)")
        except Exception as exc:
            self.log("error", f"save failed: {exc}")

    def _show_ref(self):
        sp = self.cfg.spot
        if not sp.ref_set:
            self.lab_ref.setText("<b>not calibrated</b>: click-to-go and the stabiliser "
                                 "have no spot position to work with yet.")
            return
        if sp.ref_area <= 0 and sp.ref_jitter_px <= 0:
            self.lab_ref.setText(f"<b>set by hand</b> at ({sp.ref_x:.2f}, {sp.ref_y:.2f}) px "
                                 f"(not measured: no jitter or area recorded)")
            return
        extra = ""
        if sp.ref_d4sigma_px > 0:
            extra += f", D4σ {sp.ref_d4sigma_px:.1f} px"
        if sp.ref_rel_area > 0:
            extra += f", relative area {sp.ref_rel_area:.0f} px²"
        self.lab_ref.setText(f"<b>calibrated</b> at ({sp.ref_x:.2f}, {sp.ref_y:.2f}) px, "
                             f"±{sp.ref_jitter_px:.2f} px jitter, area {sp.ref_area:.0f} px²"
                             + extra)

    # -- spot-area trace -------------------------------------------------------------
    def record(self, status, now: float | None = None):
        """Append this frame's spot area (0 = not seen). Called on every refresh,
        whichever tab is showing, so the trace has no holes; one sample per NEW
        frame, because the GUI polls faster than frames arrive."""
        if self.chk_area_pause.isChecked():
            return
        fn = getattr(status, "frame_number", None)
        if fn is None or fn == self._area_last_frame:
            return
        self._area_last_frame = fn
        if self.cfg.spot.size_method == "threshold":
            area = float(status.spot_area) if getattr(status, "spot_found", False) else 0.0
        else:
            # the brain's number for the chosen method; NaN (not measured) = 0
            v = float(getattr(status, "spot_size", float("nan")))
            area = v if np.isfinite(v) else 0.0
        self._area.append((time.monotonic() if now is None else now, area))

    def _clear_area(self):
        self._area.clear()
        self._draw_area()

    def _draw_area(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        pts = [(t - now, a) for t, a in self._area if now - t <= AREA_WINDOW_S]
        if not pts:
            self.area_plot.clear()
            self.lab_area.setText("no samples yet")
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        series = []
        sp = self.cfg.spot
        method = sp.size_method if sp.size_method in SIZE_UNITS else "threshold"
        unit = SIZE_UNITS[method]
        ref = {"threshold": sp.ref_area, "relative": sp.ref_rel_area,
               "d4sigma": sp.ref_d4sigma_px}[method]
        self.area_plot._ylabel = ("spot area px²" if method == "threshold" else
                                  "relative area px²" if method == "relative" else
                                  "spot D4σ px")
        if sp.ref_set and ref > 0:           # the reference FIRST, so the data draws on top
            series.append(([-AREA_WINDOW_S, 0.0], [ref, ref], T.COLORS["muted"], "calibrated"))
        series.append((xs, ys, SPOT_GREEN, "area" if method != "d4sigma" else "D4σ"))
        # area starts at 0 (0 = not seen); time axis fixed to the window
        self.area_plot.set_series(series, x_range=(-AREA_WINDOW_S, 0.0), y_min=0.0)
        seen = [a for a in ys if a > 0]
        if seen:
            mean, std = float(np.mean(seen)), float(np.std(seen))
            self.lab_area.setText(f"last {len(ys)} frames: {mean:.1f} ± {std:.1f} {unit} "
                                  f"({100.0 * std / mean if mean else 0:.1f} %), "
                                  f"seen in {100.0 * len(seen) / len(ys):.0f} %")
        else:
            self.lab_area.setText(f"last {len(ys)} frames: spot not seen")

    # -- live readout: numbers from status only, no image work ---------------------
    def update_status(self, status):
        self._status = status
        self._draw_area()
        sp = self.cfg.spot
        if not getattr(status, "spot_found", False):
            where = "around the calibrated position" if sp.ref_set else "in the frame"
            self.lab_live.setText(f"spot NOT seen by the threshold {where} this frame"
                                  + self._free_sizes(status))
            return
        txt = f"seen · area {status.spot_area:.0f} px²"
        if sp.ref_set and sp.ref_area > 0:
            txt += f" ({100.0 * status.spot_area / sp.ref_area:.0f} % of calibrated)"
        if sp.ref_set:
            d = float(np.hypot(status.spot_live_x - sp.ref_x, status.spot_live_y - sp.ref_y))
            txt += f" · live centroid {d:.2f} px from calibrated"
        self.lab_live.setText(txt + self._free_sizes(status))

    @staticmethod
    def _free_sizes(status) -> str:
        """This frame's threshold-free sizes from status (no image work)."""
        d4 = float(getattr(status, "spot_d4sigma_px", float("nan")))
        rel = float(getattr(status, "spot_rel_area", float("nan")))
        out = []
        if np.isfinite(d4):
            out.append(f"D4σ {d4:.1f} px (σ² {float(status.spot_sigma2_px2):.1f} px²)")
        if np.isfinite(rel):
            out.append(f"relative area {rel:.0f} px²")
        if getattr(status, "spot_saturated", False):
            out.append("SATURATED")
        return ("<br>" + " · ".join(out)) if out else ""
