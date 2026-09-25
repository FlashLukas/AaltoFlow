"""Sample-overview pane: load an image, calibrate it, click to drive the stage.

Workflow (matches how you calibrate a sample map on a microscope):

  1. Load an overview image (PNG / JPG / TIFF).
  2. Rotate it so the sample axes line up with how the stage moves.
  3. Draw a line of known physical length -> that sets the scale (mm per pixel),
     and the line's START point is pinned to the stage's CURRENT position.
  4. Now click anywhere on the picture and the stage drives to that point
     (clamped to the travel limits, which are drawn as a rectangle).

Design notes
------------
* The mapping is kept SEPARATE from the abstract 2x2 logical transform. This is
  a concrete pixel->stage navigation aid, not the logical coordinate frame.
* Rotation is "baked into" the working image (we rotate the QImage itself), so
  once rotated, pixel coordinates map to the stage with just a scale + optional
  axis flips -- no leftover rotation term to reason about. Because a rotation
  change moves every pixel, changing the rotation clears an existing
  calibration (you re-draw the line).
* The pure mapping/calibration functions below take no Qt types, so they are
  unit-tested without a display.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, axis_limits
from . import theme


# --------------------------------------------------------------------------- #
# pure calibration model + mapping (no Qt -> unit-testable)
# --------------------------------------------------------------------------- #
@dataclass
class SampleCalibration:
    """Pixel <-> stage mapping for a (possibly rotated) overview image.

    All pixel coordinates are in the ROTATED working-image frame.  The stage
    coordinates are DEVICE coordinates (mm).
    """

    image_path: str = ""
    rotation_deg: float = 0.0
    flip_x: bool = False
    flip_y: bool = True          # image Y runs downward; stage Y usually upward
    scale_mm_per_px: float = 0.0  # 0 => not calibrated
    anchor_px: float = 0.0        # line start, pixels
    anchor_py: float = 0.0
    anchor_x: float = 0.0         # stage position pinned to the line start, mm
    anchor_y: float = 0.0
    calibrated: bool = False


def compute_scale(start_px, end_px, length_mm: float) -> float:
    """mm-per-pixel from a drawn line of known physical length."""
    dx = end_px[0] - start_px[0]
    dy = end_px[1] - start_px[1]
    pixel_len = math.hypot(dx, dy)
    if pixel_len <= 0 or length_mm <= 0:
        raise ValueError("line must have non-zero length and a positive mm value")
    return length_mm / pixel_len


def image_to_stage(cal: SampleCalibration, px: float, py: float) -> tuple[float, float]:
    """Map a working-image pixel to stage device coordinates (mm)."""
    dx = px - cal.anchor_px
    dy = py - cal.anchor_py
    if cal.flip_x:
        dx = -dx
    if cal.flip_y:
        dy = -dy
    return (cal.anchor_x + cal.scale_mm_per_px * dx,
            cal.anchor_y + cal.scale_mm_per_px * dy)


def stage_to_image(cal: SampleCalibration, x: float, y: float) -> tuple[float, float]:
    """Inverse of :func:`image_to_stage` (mm -> pixels)."""
    if cal.scale_mm_per_px <= 0:
        raise ValueError("not calibrated")
    dx = (x - cal.anchor_x) / cal.scale_mm_per_px
    dy = (y - cal.anchor_y) / cal.scale_mm_per_px
    if cal.flip_x:
        dx = -dx
    if cal.flip_y:
        dy = -dy
    return (cal.anchor_px + dx, cal.anchor_py + dy)


# --------------------------------------------------------------------------- #
# the drawing canvas (image + overlays + mouse)
# --------------------------------------------------------------------------- #
class _Canvas(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(360, 300)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._mode = "nav"           # "nav" | "calibrate"
        self._line = None            # (start_px, end_px) to display
        self._marker = None          # (px, py) current stage position
        self._limits_poly = None     # list[(px,py)] travel-limit rectangle
        self._dragging = False
        self._drag_start_px = None
        self._press_widget = None
        # callbacks set by ImagePane
        self.on_line = lambda start, end: None
        self.on_click = lambda px, py: None

    # -- state from ImagePane --------------------------------------------- #
    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.setCursor(Qt.CrossCursor if mode == "calibrate" else Qt.PointingHandCursor)
        self.update()

    def set_line(self, line) -> None:
        self._line = line
        self.update()

    def set_marker(self, marker) -> None:
        self._marker = marker
        self.update()

    def set_limits_polygon(self, poly) -> None:
        self._limits_poly = poly
        self.update()

    # -- geometry: widget <-> image pixels -------------------------------- #
    def _geometry(self):
        if self._image is None:
            return None
        iw, ih = self._image.width(), self._image.height()
        if iw == 0 or ih == 0:
            return None
        w, h = self.width(), self.height()
        k = min(w / iw, h / ih)
        if k <= 0:
            return None
        offx = (w - iw * k) / 2.0
        offy = (h - ih * k) / 2.0
        return k, offx, offy, iw, ih

    def _widget_to_image(self, wx, wy):
        g = self._geometry()
        if g is None:
            return None
        k, offx, offy, iw, ih = g
        ipx = (wx - offx) / k
        ipy = (wy - offy) / k
        if ipx < 0 or ipy < 0 or ipx > iw or ipy > ih:
            return None
        return (ipx, ipy)

    def _image_to_widget(self, px, py):
        g = self._geometry()
        if g is None:
            return None
        k, offx, offy, _iw, _ih = g
        return (offx + px * k, offy + py * k)

    # -- mouse ------------------------------------------------------------ #
    def mousePressEvent(self, ev):
        p = self._widget_to_image(ev.position().x(), ev.position().y())
        if p is None:
            return
        self._press_widget = (ev.position().x(), ev.position().y())
        if self._mode == "calibrate":
            self._dragging = True
            self._drag_start_px = p
            self._line = (p, p)
            self.update()

    def mouseMoveEvent(self, ev):
        if self._dragging:
            p = self._widget_to_image(ev.position().x(), ev.position().y())
            if p is not None:
                self._line = (self._drag_start_px, p)
                self.update()

    def mouseReleaseEvent(self, ev):
        if self._mode == "calibrate" and self._dragging:
            self._dragging = False
            p = self._widget_to_image(ev.position().x(), ev.position().y())
            if p is not None and self._drag_start_px is not None:
                self._line = (self._drag_start_px, p)
                self.on_line(self._drag_start_px, p)
            return
        # navigation click: only if the pointer barely moved
        if self._mode == "nav" and self._press_widget is not None:
            moved = math.hypot(ev.position().x() - self._press_widget[0],
                               ev.position().y() - self._press_widget[1])
            p = self._widget_to_image(ev.position().x(), ev.position().y())
            if p is not None and moved < 6:
                self.on_click(*p)

    # -- painting --------------------------------------------------------- #
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.COLORS["code_bg"]))
        if self._image is None:
            p.setPen(QColor(theme.COLORS["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Load a sample overview image\n(PNG / JPG / TIFF)")
            p.end()
            return

        g = self._geometry()
        k, offx, offy, iw, ih = g
        target = QRectF(offx, offy, iw * k, ih * k)
        p.drawImage(target, self._image)

        # travel-limit rectangle
        if self._limits_poly:
            poly = QPolygonF([QPointF(*self._image_to_widget(px, py)) for px, py in self._limits_poly])
            p.setPen(QPen(QColor(theme.COLORS["ok"]), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(poly)

        # calibration line
        if self._line is not None:
            a = self._image_to_widget(*self._line[0])
            b = self._image_to_widget(*self._line[1])
            if a and b:
                p.setPen(QPen(QColor(theme.COLORS["accent_hi"]), 2))
                p.drawLine(QPointF(*a), QPointF(*b))
                for pt in (a, b):
                    p.setBrush(QColor(theme.COLORS["accent"]))
                    p.drawEllipse(QPointF(*pt), 4, 4)

        # current-position marker (crosshair)
        if self._marker is not None:
            m = self._image_to_widget(*self._marker)
            if m:
                mx, my = m
                p.setPen(QPen(QColor(theme.COLORS["accent"]), 2))
                p.drawLine(QPointF(mx - 10, my), QPointF(mx + 10, my))
                p.drawLine(QPointF(mx, my - 10), QPointF(mx, my + 10))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(mx, my), 7, 7)
        p.end()


# --------------------------------------------------------------------------- #
# the pane: toolbar + canvas + calibration logic
# --------------------------------------------------------------------------- #
class ImagePane(QWidget):
    def __init__(self, ctrl, cfg: Config, log=None):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._log = log or (lambda level, msg: None)

        self._orig: QImage | None = None
        self._work: QImage | None = None
        self._rotation = 0.0
        self._cal = SampleCalibration()

        self._build_ui()

    # -- UI --------------------------------------------------------------- #
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        title = QLabel("SAMPLE OVERVIEW")
        title.setObjectName("cardTitle")
        lay.addWidget(title)

        # toolbar row 1: load + rotate
        row1 = QHBoxLayout()
        load = QPushButton("Load image…")
        load.clicked.connect(self._load_dialog)
        row1.addWidget(load)
        rot_m = QPushButton("⟲ 90°")
        rot_p = QPushButton("⟳ 90°")
        rot_m.clicked.connect(lambda: self._nudge_rotation(-90))
        rot_p.clicked.connect(lambda: self._nudge_rotation(+90))
        row1.addWidget(rot_m)
        row1.addWidget(rot_p)
        row1.addWidget(QLabel("angle"))
        self._rot_spin = QDoubleSpinBox()
        self._rot_spin.setRange(-180, 180)
        self._rot_spin.setDecimals(1)
        self._rot_spin.setSingleStep(1.0)
        self._rot_spin.setSuffix("°")
        self._rot_spin.valueChanged.connect(self._on_rot_spin)
        row1.addWidget(self._rot_spin)
        row1.addStretch(1)
        lay.addLayout(row1)

        # toolbar row 2: calibrate + flips + save/load calibration
        row2 = QHBoxLayout()
        self._cal_btn = QPushButton("Calibrate (draw line)")
        self._cal_btn.setObjectName("primary")
        theme.repolish(self._cal_btn)
        self._cal_btn.clicked.connect(self._start_calibrate)
        row2.addWidget(self._cal_btn)
        self._flip_x = QCheckBox("flip X")
        self._flip_y = QCheckBox("flip Y")
        self._flip_y.setChecked(True)
        self._flip_x.toggled.connect(self._on_flip)
        self._flip_y.toggled.connect(self._on_flip)
        row2.addWidget(self._flip_x)
        row2.addWidget(self._flip_y)
        row2.addStretch(1)
        save = QPushButton("Save cal…")
        load_c = QPushButton("Load cal…")
        save.clicked.connect(self._save_cal_dialog)
        load_c.clicked.connect(self._load_cal_dialog)
        row2.addWidget(save)
        row2.addWidget(load_c)
        lay.addLayout(row2)

        self._canvas = _Canvas()
        self._canvas.on_line = self._on_line_drawn
        self._canvas.on_click = self._on_nav_click
        lay.addWidget(self._canvas, 1)

        self._status = QLabel("No image loaded.")
        self._status.setObjectName("muted")
        lay.addWidget(self._status)

        outer.addWidget(card)

    # -- image loading ---------------------------------------------------- #
    def _load_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sample overview image", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp);;All files (*)",
        )
        if path:
            self.load_image(path)

    def _read_image(self, path: str) -> QImage | None:
        img = QImage(path)
        if not img.isNull():
            return img
        # Fallback (esp. multipage / 16-bit TIFF): Pillow -> raw RGBA -> QImage.
        try:
            from PIL import Image  # lazy; only needed when Qt can't read it
        except ImportError:
            self._log("error", "cannot read this image; install 'pillow' for TIFF support")
            return None
        try:
            pil = Image.open(path).convert("RGBA")
            data = pil.tobytes("raw", "RGBA")
            qimg = QImage(data, pil.width, pil.height, QImage.Format_RGBA8888)
            return qimg.copy()  # copy so QImage owns the buffer
        except Exception as exc:
            self._log("error", f"failed to read image: {exc}")
            return None

    def load_image(self, path: str) -> bool:
        img = self._read_image(path)
        if img is None or img.isNull():
            return False
        self._orig = img
        self._cal.image_path = path
        self._rebuild_working()
        self._clear_calibration("new image loaded")
        self._log("info", f"loaded overview image ({img.width()}×{img.height()} px)")
        return True

    def _rebuild_working(self):
        if self._orig is None:
            self._work = None
            self._canvas.set_image(None)
            return
        t = QTransform()
        t.rotate(self._rotation)
        self._work = self._orig.transformed(t, Qt.SmoothTransformation)
        self._canvas.set_image(self._work)
        self._refresh_overlays()

    # -- rotation --------------------------------------------------------- #
    def set_rotation(self, deg: float, clear: bool = True):
        self._rotation = float(deg)
        self._rot_spin.blockSignals(True)
        self._rot_spin.setValue(self._rotation)
        self._rot_spin.blockSignals(False)
        self._rebuild_working()
        if clear:
            self._clear_calibration("image rotated")

    def _nudge_rotation(self, delta):
        new = self._rotation + delta
        while new > 180:
            new -= 360
        while new < -180:
            new += 360
        self.set_rotation(new, clear=True)

    def _on_rot_spin(self, value):
        self.set_rotation(value, clear=True)

    # -- calibration ------------------------------------------------------ #
    def _start_calibrate(self):
        if self._work is None:
            self._log("warn", "load an image first")
            return
        self._canvas.set_mode("calibrate")
        self._status.setText("Draw a line of known length on the image…")

    def _on_line_drawn(self, start, end):
        # Ask the physical length of the line the user just drew.
        length, ok = QInputDialog.getDouble(
            self, "Calibration", "Length of the drawn line (mm):",
            1.0, 0.0001, 1e6, 4)
        self._canvas.set_mode("nav")
        if not ok:
            self._status.setText("Calibration cancelled.")
            return
        try:
            self.apply_calibration(start, end, length)
        except Exception as exc:
            self._log("error", f"calibration failed: {exc}")

    def apply_calibration(self, start_px, end_px, length_mm, anchor_stage=None) -> bool:
        """Set the pixel->stage mapping from a drawn line.

        The line's START point is pinned to ``anchor_stage`` (defaults to the
        stage's CURRENT position).  Separated from the GUI dialog so tests can
        call it directly.
        """
        scale = compute_scale(start_px, end_px, length_mm)
        if anchor_stage is None:
            st = self.ctrl.status()
            anchor_stage = (st.position[0], st.position[1])
        self._cal.scale_mm_per_px = scale
        self._cal.anchor_px, self._cal.anchor_py = float(start_px[0]), float(start_px[1])
        self._cal.anchor_x, self._cal.anchor_y = float(anchor_stage[0]), float(anchor_stage[1])
        self._cal.rotation_deg = self._rotation
        self._cal.flip_x = self._flip_x.isChecked()
        self._cal.flip_y = self._flip_y.isChecked()
        self._cal.calibrated = True
        self._canvas.set_line((tuple(map(float, start_px)), tuple(map(float, end_px))))
        self._refresh_overlays()
        self._status.setText(
            f"Calibrated: {scale:.4g} mm/px · origin pinned to "
            f"({anchor_stage[0]:.4g}, {anchor_stage[1]:.4g}) mm. Click to move."
        )
        self._log("info", f"image calibrated: {scale:.4g} mm/px")
        return True

    def _clear_calibration(self, reason=""):
        was = self._cal.calibrated
        self._cal.scale_mm_per_px = 0.0
        self._cal.calibrated = False
        self._canvas.set_line(None)
        self._canvas.set_marker(None)
        self._canvas.set_limits_polygon(None)
        if self._work is None:
            self._status.setText("No image loaded.")
        else:
            self._status.setText("Image loaded — rotate if needed, then Calibrate.")
        if was and reason:
            self._log("warn", f"calibration cleared ({reason})")

    def _on_flip(self, _checked):
        if self._cal.calibrated:
            self._cal.flip_x = self._flip_x.isChecked()
            self._cal.flip_y = self._flip_y.isChecked()
            self._refresh_overlays()

    # -- navigation ------------------------------------------------------- #
    def _on_nav_click(self, px, py):
        if not self._cal.calibrated:
            self._log("warn", "calibrate the image before clicking to move")
            return
        x, y = image_to_stage(self._cal, px, py)
        try:
            self.ctrl.move_axis(0, x)
            self.ctrl.move_axis(1, y)
            self._log("info", f"image click → stage ({x:.4g}, {y:.4g}) mm")
        except Exception as exc:
            self._log("error", f"{type(exc).__name__}: {exc}")

    # -- overlays (marker + limit rectangle), refreshed on poll ----------- #
    def update_marker(self):
        if not self._cal.calibrated or self._work is None:
            return
        try:
            st = self.ctrl.status()
            px, py = stage_to_image(self._cal, st.position[0], st.position[1])
            self._canvas.set_marker((px, py))
        except Exception:
            pass

    def _refresh_overlays(self):
        if not self._cal.calibrated or self._work is None:
            self._canvas.set_limits_polygon(None)
            return
        lo_x, hi_x = axis_limits(self.cfg, 0)
        lo_y, hi_y = axis_limits(self.cfg, 1)
        corners = [(lo_x, lo_y), (hi_x, lo_y), (hi_x, hi_y), (lo_x, hi_y)]
        try:
            poly = [stage_to_image(self._cal, x, y) for x, y in corners]
            self._canvas.set_limits_polygon(poly)
        except Exception:
            self._canvas.set_limits_polygon(None)
        self.update_marker()

    # -- save / load calibration ------------------------------------------ #
    def _save_cal_dialog(self):
        if not self._cal.calibrated:
            self._log("warn", "nothing to save — calibrate first")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save calibration", "calibration.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(asdict(self._cal), fh, indent=2)
            self._log("info", f"saved calibration → {path}")

    def _load_cal_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load calibration", "", "JSON (*.json)")
        if path:
            self.load_calibration(path)

    def load_calibration(self, path: str) -> bool:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cal = SampleCalibration(**{k: data[k] for k in data if k in SampleCalibration().__dict__})
        # Re-open the image and re-apply the same rotation so pixel coords match.
        if cal.image_path:
            self._rotation = cal.rotation_deg
            if not self.load_image(cal.image_path):
                self._log("error", "calibration's image could not be reopened")
                return False
        self._rotation = cal.rotation_deg
        self._rot_spin.blockSignals(True)
        self._rot_spin.setValue(self._rotation)
        self._rot_spin.blockSignals(False)
        self._rebuild_working()
        self._flip_x.setChecked(cal.flip_x)
        self._flip_y.setChecked(cal.flip_y)
        self._cal = cal
        self._refresh_overlays()
        self._status.setText(f"Calibration loaded: {cal.scale_mm_per_px:.4g} mm/px. Click to move.")
        self._log("info", f"loaded calibration ← {path}")
        return True

    # -- external hook: limits changed in settings ------------------------ #
    def on_limits_changed(self):
        self._refresh_overlays()
