"""The Spot tab: define the laser spot by thresholding, then CALIBRATE its position.

How the spot is used (decided with Lukas, 2026-09-13):

  * POSITION is a user decision. The laser spot is fixed in the image, so you
    set the threshold here, press "Calibrate spot", and that averaged centroid
    is THE spot position -- the one click-to-go and the stabiliser use -- until
    you calibrate again. It is not re-derived from every frame.
  * SIZE is evaluated every frame by the brain, thresholding only a small search
    box around the calibrated position ("search region"). This tab shows that
    live size and whether the live centroid has wandered from the calibration.

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
        p.setPen(QColor(T.COLORS["muted"]))
        p.drawText(6, self.height() - 6, f"{sw}x{sh} px · cross: position · ring: detected")
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

        f, l = _card("Live  (the brain's per-frame size check)")
        self.lab_live = QLabel("-"); self.lab_live.setWordWrap(True)
        l.addWidget(self.lab_live)
        right.addWidget(f)
        right.addStretch(1)
        self._show_ref()

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
                "lookup_region_y_px": self.sp_look_y.value()}

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
        self.lab_ref.setText(f"<b>calibrated</b> at ({sp.ref_x:.2f}, {sp.ref_y:.2f}) px, "
                             f"±{sp.ref_jitter_px:.2f} px jitter, area {sp.ref_area:.0f} px²")

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
        area = float(status.spot_area) if getattr(status, "spot_found", False) else 0.0
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
        if sp.ref_set and sp.ref_area > 0:   # the reference FIRST, so the data draws on top
            series.append(([-AREA_WINDOW_S, 0.0], [sp.ref_area, sp.ref_area],
                           T.COLORS["muted"], "calibrated"))
        series.append((xs, ys, SPOT_GREEN, "area"))
        # area starts at 0 (0 = not seen); time axis fixed to the window
        self.area_plot.set_series(series, x_range=(-AREA_WINDOW_S, 0.0), y_min=0.0)
        seen = [a for a in ys if a > 0]
        if seen:
            mean, std = float(np.mean(seen)), float(np.std(seen))
            self.lab_area.setText(f"last {len(ys)} frames: {mean:.0f} ± {std:.0f} px² "
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
            self.lab_live.setText(f"spot NOT seen {where} this frame")
            return
        txt = f"seen · area {status.spot_area:.0f} px²"
        if sp.ref_set and sp.ref_area > 0:
            txt += f" ({100.0 * status.spot_area / sp.ref_area:.0f} % of calibrated)"
        if sp.ref_set:
            d = float(np.hypot(status.spot_live_x - sp.ref_x, status.spot_live_y - sp.ref_y))
            txt += f" · live centroid {d:.2f} px from calibrated"
        self.lab_live.setText(txt)
