"""CameraView -- the signature widget for this module (blueprint §7).

Where the magnet has a dipole glyph and the RF gen has a radiating antenna, the
camera module's personality IS its live image with the tracking overlays painted
on top: the laser-spot crosshair, the matched template box, the scanning-point
array, the currently selected point, the pattern 'safety area', and the stage
travel range.  It reads instantly what the feedback system is doing.

It also turns mouse clicks into image-pixel coordinates (letter-box aware) and
emits them, so the window can wire "click to go" and template-ROI selection to
it without the widget needing to know the brain.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from . import theme as T
from .. import vision as V

# Spot overlays are GREEN with a dark outline (Lukas, 2026-09-13: amber/red were
# hard to read). They sit on a grayscale camera image whatever the GUI theme, so
# these are deliberately theme-independent -- not palette entries.
SPOT_GREEN = "#2bff6a"                 # the spot position (crosshair) + detected box
SPOT_TINT_BGRA = (106, 255, 43, 105)   # thresholded pixels, same green, translucent
OUTLINE = QColor(0, 0, 0, 200)         # under every green line: readable on white too
AIM_COLOUR = "#ff4fd8"                 # the stabiliser's target (selected scan point)


def outlined_pen(p: QPainter, colour: str, width: float, style=Qt.SolidLine):
    """Draw the next shape twice: call ``draw()`` after each pen this yields."""
    under = QPen(OUTLINE, width + 2.5)
    under.setStyle(style)
    under.setCapStyle(Qt.RoundCap)
    over = QPen(QColor(colour), width)
    over.setStyle(style)
    over.setCapStyle(Qt.RoundCap)
    for pen in (under, over):
        p.setPen(pen)
        yield


class CameraView(QWidget):
    clicked = Signal(float, float)          # image-pixel (x, y) of a left click
    roi_selected = Signal(float, float, float, float)      # template ROI drag
    # scan-area rectangle: cx, cy, w, h (px), angle (deg)
    scan_area_selected = Signal(float, float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 360)
        self._img: QImage | None = None
        self._buf = None                    # keep the numpy buffer alive
        self._frame_w = 640
        self._frame_h = 480
        self._draw_rect = QRectF(0, 0, 1, 1)  # where the image is drawn (widget px)
        self._scale = 1.0
        self._status = None
        self._cfg = None
        self._roi_mode = False
        self._scan_mode = False
        self._show_threshold = False
        self._show_spot_info = False
        self._show_pattern_info = False
        self._show_scan_points = True
        self._drag_start = None
        self._drag_now = None
        # interactive scan-area editor
        self._scan_edit = None       # live {cx,cy,w,h,angle} (px/deg) while dragging
        self._scan_kind = None       # 'new' | 'move' | 'resize' | 'rotate'
        self._scan_last = None       # last image point (for move deltas)
        self._scan_new_start = None  # anchor for a fresh rectangle
        self._scan_rect = None       # persisted committed/recalled rectangle (px/deg)
        self.setMouseTracking(True)

    # -- data in ----------------------------------------------------------- #
    def set_frame(self, gray: np.ndarray | None) -> None:
        if gray is None:
            return
        if gray.ndim == 3:
            gray = gray[..., 0]
        gray = np.ascontiguousarray(gray, dtype=np.uint8)
        self._buf = gray
        self._frame_h, self._frame_w = gray.shape
        self._img = QImage(self._buf.data, self._frame_w, self._frame_h,
                           self._frame_w, QImage.Format_Grayscale8)
        self.update()

    def set_overlay(self, status, cfg) -> None:
        self._status = status
        self._cfg = cfg
        self.update()

    def set_roi_mode(self, on: bool) -> None:
        self._roi_mode = bool(on)
        if on:
            self._scan_mode = False

    def set_scan_mode(self, on: bool) -> None:
        self._scan_mode = bool(on)
        if on:
            self._roi_mode = False
        else:
            self._scan_edit = self._scan_kind = None
        self.update()

    def set_recalled_rect(self, rect: dict | None) -> None:
        """Show a rectangle rebuilt from the known ROI so it can be edited."""
        self._scan_rect = dict(rect) if rect else None
        if rect is not None:
            self._scan_mode = True
            self._roi_mode = False
        self.update()

    def clear_scan_rect(self) -> None:
        self._scan_rect = self._scan_edit = self._scan_kind = None
        self.update()

    def set_show_threshold(self, on: bool) -> None:
        self._show_threshold = bool(on)
        self.update()

    def set_show_spot_info(self, on: bool) -> None:
        """Spot area as text next to the spot."""
        self._show_spot_info = bool(on)
        self.update()

    def set_show_scan_points(self, on: bool) -> None:
        self._show_scan_points = bool(on)
        self.update()

    def set_show_pattern_info(self, on: bool) -> None:
        """Match score, position and distance to the spot under the pattern box."""
        self._show_pattern_info = bool(on)
        self.update()

    # -- coordinate mapping ------------------------------------------------ #
    def _img_to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self._draw_rect.left() + x * self._scale,
                       self._draw_rect.top() + y * self._scale)

    def _widget_to_img(self, x: float, y: float) -> tuple:
        ix = (x - self._draw_rect.left()) / self._scale
        iy = (y - self._draw_rect.top()) / self._scale
        return (ix, iy)

    # -- mouse ------------------------------------------------------------- #
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        ipt = self._widget_to_img(ev.position().x(), ev.position().y())
        if self._scan_mode:
            self._scan_press(ipt)
        elif self._roi_mode:
            self._drag_start = ev.position()
            self._drag_now = ev.position()
        else:
            if 0 <= ipt[0] < self._frame_w and 0 <= ipt[1] < self._frame_h:
                self.clicked.emit(ipt[0], ipt[1])

    def mouseMoveEvent(self, ev):
        if self._scan_mode and self._scan_kind is not None:
            self._scan_move(self._widget_to_img(ev.position().x(), ev.position().y()))
        elif self._roi_mode and self._drag_start is not None:
            self._drag_now = ev.position()
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._scan_mode and self._scan_kind is not None:
            self._scan_release()
        elif self._roi_mode and self._drag_start is not None:
            x0, y0 = self._widget_to_img(self._drag_start.x(), self._drag_start.y())
            x1, y1 = self._widget_to_img(ev.position().x(), ev.position().y())
            self._drag_start = self._drag_now = None
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            w, h = abs(x1 - x0), abs(y1 - y0)
            if w >= 6 and h >= 6:
                self.roi_selected.emit(cx, cy, w, h)
            self.update()

    # -- scan-area editor -------------------------------------------------- #
    @staticmethod
    def _rot(lx, ly, adeg):
        ca, sa = math.cos(math.radians(adeg)), math.sin(math.radians(adeg))
        return (lx * ca - ly * sa, lx * sa + ly * ca)

    def _cfg_wh(self):
        """Rectangle width/height (px) implied by the config pitch + point count.

        Deriving the size from config (instead of freezing it in the persisted
        rect) is what makes 'Apply size', pitch edits, and point-count edits
        actually reshape the on-screen rectangle.
        """
        cfg = self._cfg
        if cfg is None:
            return None
        sc = cfg.scanning
        px_x = cfg.image.pixel_size_x_um or 1.0
        px_y = cfg.image.pixel_size_y_um or 1.0
        w = (sc.points_x - 1) * sc.dx_um / px_x if sc.points_x > 1 else 0.0
        h = (sc.points_y - 1) * sc.dy_um / px_y if sc.points_y > 1 else 0.0
        return (w, h)

    def _scan_rect_from_state(self):
        """The current scan-area rectangle {cx,cy,w,h,angle} in image px/deg.

        Priority: a live drag > the persisted rect (placement) > a rect computed
        from the live status.  In all non-drag cases the SIZE and ANGLE come from
        the config, so numeric edits reshape the rectangle immediately.
        """
        if self._scan_edit is not None:
            return self._scan_edit
        cfg = self._cfg
        wh = self._cfg_wh()
        if self._scan_rect is not None:      # persisted placement (cx, cy)
            r = dict(self._scan_rect)
            if wh is not None:
                r["w"], r["h"] = wh
            if cfg is not None:
                r["angle"] = cfg.scanning.angle_deg
            return r
        s = self._status
        if s is None or cfg is None or not getattr(s, "match_found", False):
            return None
        sc = cfg.scanning
        px_x, px_y = cfg.image.pixel_size_x_um, cfg.image.pixel_size_y_um
        offs = V.scanning_array_pixel_offsets(sc.points_x, sc.points_y, sc.dx_um,
                                              sc.dy_um, sc.angle_deg, px_x, px_y)
        six = int(np.clip(sc.selected_index_x, 0, sc.points_x - 1))
        siy = int(np.clip(sc.selected_index_y, 0, sc.points_y - 1))
        cx = s.selected_point_x - offs[siy, six, 0]
        cy = s.selected_point_y - offs[siy, six, 1]
        w, h = wh if wh is not None else (0.0, 0.0)
        return {"cx": cx, "cy": cy, "w": w, "h": h, "angle": sc.angle_deg}

    def _rect_corners_img(self, r):
        out = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            rx, ry = self._rot(sx * r["w"] / 2, sy * r["h"] / 2, r["angle"])
            out.append((r["cx"] + rx, r["cy"] + ry))
        return out

    def _rect_handle_img(self, r):
        off = 22.0 / max(self._scale, 1e-6)
        rx, ry = self._rot(0.0, -(r["h"] / 2 + off), r["angle"])
        return (r["cx"] + rx, r["cy"] + ry)

    def _scan_press(self, pt):
        r = self._scan_rect_from_state()
        tol = 11.0 / max(self._scale, 1e-6)
        if r is not None and (r["w"] > 0 or r["h"] > 0):
            hx, hy = self._rect_handle_img(r)
            if math.hypot(pt[0] - hx, pt[1] - hy) < tol:
                self._scan_kind, self._scan_edit = "rotate", dict(r)
                return
            for cxp, cyp in self._rect_corners_img(r):
                if math.hypot(pt[0] - cxp, pt[1] - cyp) < tol:
                    self._scan_kind, self._scan_edit = "resize", dict(r)
                    return
            lx, ly = self._rot(pt[0] - r["cx"], pt[1] - r["cy"], -r["angle"])
            if abs(lx) <= r["w"] / 2 and abs(ly) <= r["h"] / 2:
                self._scan_kind, self._scan_edit, self._scan_last = "move", dict(r), pt
                return
        # otherwise start a brand-new axis-aligned rectangle
        self._scan_kind = "new"
        self._scan_new_start = pt
        self._scan_edit = {"cx": pt[0], "cy": pt[1], "w": 0.0, "h": 0.0, "angle": 0.0}

    def _scan_move(self, pt):
        k, e = self._scan_kind, self._scan_edit
        if e is None:
            return
        if k == "new":
            x0, y0 = self._scan_new_start
            e["cx"], e["cy"] = (x0 + pt[0]) / 2, (y0 + pt[1]) / 2
            e["w"], e["h"], e["angle"] = abs(pt[0] - x0), abs(pt[1] - y0), 0.0
        elif k == "move":
            e["cx"] += pt[0] - self._scan_last[0]
            e["cy"] += pt[1] - self._scan_last[1]
            self._scan_last = pt
        elif k == "resize":
            lx, ly = self._rot(pt[0] - e["cx"], pt[1] - e["cy"], -e["angle"])
            e["w"], e["h"] = max(2.0, 2 * abs(lx)), max(2.0, 2 * abs(ly))
        elif k == "rotate":
            e["angle"] = math.degrees(math.atan2(pt[0] - e["cx"], -(pt[1] - e["cy"])))
        self.update()                 # rectangle follows live; commit on release

    def _scan_release(self):
        if self._scan_edit is not None and (self._scan_edit["w"] >= 4 or
                                            self._scan_edit["h"] >= 4):
            self._emit_scan(self._scan_edit)
            # PERSIST the rectangle so it stays visible + editable after release
            # (this is what lets you draw, release, then grab the rotate handle).
            self._scan_rect = dict(self._scan_edit)
        self._scan_kind = self._scan_edit = self._scan_last = self._scan_new_start = None
        self.update()

    def _emit_scan(self, e):
        self.scan_area_selected.emit(e["cx"], e["cy"], e["w"], e["h"], e["angle"])

    # -- painting ---------------------------------------------------------- #
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.BG))
        w, h = self.width(), self.height()

        # Fit the frame into the widget, preserving aspect ratio (letter-box).
        self._scale = min(w / self._frame_w, h / self._frame_h)
        dw, dh = self._frame_w * self._scale, self._frame_h * self._scale
        left, top = (w - dw) / 2.0, (h - dh) / 2.0
        self._draw_rect = QRectF(left, top, dw, dh)

        if self._img is not None:
            p.drawImage(self._draw_rect, self._img)
            if self._show_threshold:
                self._paint_threshold(p)
        else:
            p.setPen(QColor(T.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "no frame")

        p.setRenderHint(QPainter.Antialiasing, True)
        self._paint_overlays(p)
        self._paint_scan_rect(p)

        # In-progress template-ROI rubber-band.
        if self._roi_mode and self._drag_start is not None and self._drag_now is not None:
            pen = QPen(QColor(T.ACCENT_HI)); pen.setStyle(Qt.DashLine); pen.setWidth(1)
            p.setPen(pen)
            p.drawRect(QRectF(self._drag_start, self._drag_now))
        p.end()

    def _paint_scan_rect(self, p: QPainter):
        """Draw the scan-area rectangle (rotated) and, in scan mode, its handles."""
        r = self._scan_rect_from_state()
        if r is None or (r["w"] <= 0 and r["h"] <= 0):
            return
        corners = [self._img_to_widget(*c) for c in self._rect_corners_img(r)]
        editing = self._scan_mode
        pen = QPen(QColor(T.ACCENT), 2 if editing else 1)
        if not editing:
            pen.setStyle(Qt.DotLine)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawPolygon(QPolygonF(corners))
        if not editing:
            return
        # rotation handle: a stalk up from the top edge with a knob
        top_mid = QPointF((corners[0].x() + corners[1].x()) / 2,
                          (corners[0].y() + corners[1].y()) / 2)
        hpt = self._img_to_widget(*self._rect_handle_img(r))
        p.setPen(QPen(QColor(T.ACCENT), 1))
        p.drawLine(top_mid, hpt)
        p.setBrush(QColor(T.ACCENT_HI))
        p.drawEllipse(hpt, 5, 5)
        # corner resize handles
        p.setBrush(QColor(T.ACCENT))
        for c in corners:
            p.drawRect(QRectF(c.x() - 4, c.y() - 4, 8, 8))
        p.setBrush(Qt.NoBrush)

    def _paint_threshold(self, p: QPainter):
        """Tint the spot-threshold pixels green (LabVIEW 'Threshold image' /
        'Show spot area'), computed straight from the frame + config thresholds."""
        if self._buf is None or self._cfg is None:
            return
        lo = int(self._cfg.spot.thr_lower)
        hi = int(self._cfg.spot.thr_upper)
        buf = self._buf
        if self._cfg.spot.bright_spot:
            mask = (buf >= lo) & (buf <= hi)
        else:
            mask = (buf <= (255 - lo)) & (buf >= (255 - hi))
        if not mask.any():
            return
        # Build an ARGB overlay: green where masked, transparent elsewhere.
        h, w = mask.shape
        argb = np.zeros((h, w, 4), np.uint8)
        argb[mask] = SPOT_TINT_BGRA
        self._thr_buf = np.ascontiguousarray(argb)
        img = QImage(self._thr_buf.data, w, h, 4 * w, QImage.Format_ARGB32)
        p.drawImage(self._draw_rect, img)

    def _paint_overlays(self, p: QPainter):
        s, cfg = self._status, self._cfg
        if s is None or cfg is None:
            return

        # spot bounding box (this frame's detected 'spot area'), at least 8 px on
        # screen so a few-pixel spot on a big frame is still visible
        if getattr(s, "spot_found", False) and getattr(s, "spot_bbox_w", 0) > 0:
            tlp = self._img_to_widget(s.spot_bbox_x, s.spot_bbox_y)
            bw = max(8.0, s.spot_bbox_w * self._scale)
            bh = max(8.0, s.spot_bbox_h * self._scale)
            cx = tlp.x() + s.spot_bbox_w * self._scale / 2
            cy = tlp.y() + s.spot_bbox_h * self._scale / 2
            p.setBrush(Qt.NoBrush)
            for _ in outlined_pen(p, SPOT_GREEN, 1.5, Qt.DashLine):
                p.drawRect(QRectF(cx - bw / 2 - 3, cy - bh / 2 - 3, bw + 6, bh + 6))

        # -- stage travel range (a box around the spot sized by motor travel) - #
        if getattr(s, "spot_calibrated", False) and cfg.image.pixel_size_x_um > 0:
            cx, cy = self._img_to_widget(s.spot_x, s.spot_y).toTuple()
            rx = (cfg.limits.motor_x_max - cfg.limits.motor_x_min) / cfg.image.pixel_size_x_um * self._scale
            ry = (cfg.limits.motor_y_max - cfg.limits.motor_y_min) / cfg.image.pixel_size_y_um * self._scale
            pen = QPen(QColor(T.ACCENT_DIM)); pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            p.drawRect(QRectF(cx - rx / 2, cy - ry / 2, rx, ry))

        # -- template match box + safety area --------------------------------- #
        # Every pattern: the DRIVER solid, other matched ones dashed, the ones not
        # matched dotted where they should be (off-screen ones are simply clipped).
        driver = int(getattr(s, "pattern_driver", 0))
        for k, box in enumerate(getattr(s, "pattern_boxes", None) or []):
            bx, by, bw, bh, matched = box[0], box[1], box[2], box[3], box[4]
            c = self._img_to_widget(bx, by)
            ww, hh = bw * self._scale, bh * self._scale
            if k == driver and matched:
                pen = QPen(QColor(T.OK), 2)
            else:
                pen = QPen(QColor(T.OK if matched else T.MUTED), 1)
                pen.setStyle(Qt.DashLine if matched else Qt.DotLine)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(c.x() - ww / 2, c.y() - hh / 2, ww, hh))
            p.drawText(QPointF(c.x() - ww / 2 + 2, c.y() - hh / 2 - 3),
                       "main" if k == 0 else f"B{k}")
        if getattr(s, "match_found", False):
            tp = self._img_to_widget(s.template_x, s.template_y)
            if not getattr(s, "pattern_boxes", None):
                tw = max(1, getattr(s, "template_w", 30)) * self._scale
                th = max(1, getattr(s, "template_h", 30)) * self._scale
                p.setPen(QPen(QColor(T.OK), 2))
                p.drawRect(QRectF(tp.x() - tw / 2, tp.y() - th / 2, tw, th))
            if not cfg.pattern.full_image:
                sa = cfg.pattern.safety_area_px * self._scale
                pen = QPen(QColor(T.MUTED)); pen.setStyle(Qt.DashLine)
                p.setPen(pen)
                p.drawRect(QRectF(tp.x() - sa, tp.y() - sa, 2 * sa, 2 * sa))

            # -- scanning-point array pinned to the template ------------------ #
            offs = V.scanning_array_pixel_offsets(
                cfg.scanning.points_x, cfg.scanning.points_y,
                cfg.scanning.dx_um, cfg.scanning.dy_um, cfg.scanning.angle_deg,
                cfg.image.pixel_size_x_um, cfg.image.pixel_size_y_um)
            acx = s.template_x  # array centre offset already baked into template? no:
            # array centre = template + stored offset; the brain reports the
            # selected point, but for ALL points we approximate the centre from
            # the selected point minus its own offset.
            six = int(np.clip(cfg.scanning.selected_index_x, 0, cfg.scanning.points_x - 1))
            siy = int(np.clip(cfg.scanning.selected_index_y, 0, cfg.scanning.points_y - 1))
            acx = s.selected_point_x - offs[siy, six, 0]
            acy = s.selected_point_y - offs[siy, six, 1]
            r = max(2.0, cfg.scanning.overlay_size * self._scale)
            for iy in range(cfg.scanning.points_y if self._show_scan_points else 0):
                for ix in range(cfg.scanning.points_x):
                    px = acx + offs[iy, ix, 0]
                    py = acy + offs[iy, ix, 1]
                    wp = self._img_to_widget(px, py)
                    sel = (ix == six and iy == siy)
                    p.setPen(QPen(QColor(T.ACCENT if sel else T.ACCENT_HI), 2))
                    if sel or cfg.scanning.overlay_style == "fill":
                        p.setBrush(QColor(T.ACCENT if sel else T.ACCENT_HI))
                    else:
                        p.setBrush(Qt.NoBrush)
                    p.drawEllipse(wp, r, r)
            p.setBrush(Qt.NoBrush)

        # -- the spot SEARCH REGION around the calibrated position ------------ #
        sp_cfg = cfg.spot
        if getattr(s, "spot_calibrated", False) and sp_cfg.lookup_region_px > 0:
            reg = V.search_region((s.spot_x, s.spot_y), sp_cfg.lookup_region_px,
                                  sp_cfg.lookup_region_y_px, sp_cfg.search_shape)
            if reg is not None:
                c = self._img_to_widget(s.spot_x, s.spot_y)
                hx, hy = reg["half"][0] * self._scale, reg["half"][1] * self._scale
                p.setBrush(Qt.NoBrush)
                for _ in outlined_pen(p, SPOT_GREEN, 1.0, Qt.DotLine):
                    if reg["shape"] == "circle":
                        p.drawEllipse(c, hx, hx)
                    else:
                        p.drawRect(QRectF(c.x() - hx, c.y() - hy, 2 * hx, 2 * hy))

        # -- laser spot crosshair at the CALIBRATED position (drawn last) ------ #
        # The dotted box above is this frame's detection (size); the crosshair
        # is the calibrated position that click-to-go and the stabiliser use.
        if getattr(s, "spot_calibrated", False):
            sp = self._img_to_widget(s.spot_x, s.spot_y)
            g = 12
            p.setBrush(Qt.NoBrush)
            for _ in outlined_pen(p, SPOT_GREEN, 2.0):
                # crosshair with a gap in the middle, so the spot itself stays visible
                p.drawLine(QPointF(sp.x() - g, sp.y()), QPointF(sp.x() - 4, sp.y()))
                p.drawLine(QPointF(sp.x() + 4, sp.y()), QPointF(sp.x() + g, sp.y()))
                p.drawLine(QPointF(sp.x(), sp.y() - g), QPointF(sp.x(), sp.y() - 4))
                p.drawLine(QPointF(sp.x(), sp.y() + 4), QPointF(sp.x(), sp.y() + g))
                if getattr(s, "stable", False):        # stabiliser on target: a ring
                    p.drawEllipse(sp, g + 4, g + 4)

        # -- where the stabiliser is AIMING: the selected scan point ----------- #
        # A small diagonal cross (an "x", so it never reads as the spot's "+"),
        # drawn while stabilising -- also when the scan points are hidden.
        if getattr(s, "stabilize_on", False) and getattr(s, "match_found", False):
            ap = self._img_to_widget(s.selected_point_x, s.selected_point_y)
            a = 7
            for _ in outlined_pen(p, AIM_COLOUR, 2.0):
                p.drawLine(QPointF(ap.x() - a, ap.y() - a), QPointF(ap.x() + a, ap.y() + a))
                p.drawLine(QPointF(ap.x() - a, ap.y() + a), QPointF(ap.x() + a, ap.y() - a))

        # -- optional text labels on the image (Imaging card checkboxes) ------- #
        # Micrometres once an objective calibration gives the pixel size (Lukáš,
        # 2026-09-14); pixels only when there is none. Positions are the image
        # coordinate x pixel size (origin = top-left of the processed frame).
        px_x, px_y = cfg.image.pixel_size_x_um, cfg.image.pixel_size_y_um
        in_um = bool(cfg.image.objective_name) and px_x > 0 and px_y > 0
        drawn: list = []                      # label boxes so far, to avoid overlaps
        if self._show_pattern_info and getattr(s, "match_found", False):
            tw = max(1, getattr(s, "template_w", 30)) * self._scale
            th = max(1, getattr(s, "template_h", 30)) * self._scale
            tp = self._img_to_widget(s.template_x, s.template_y)
            lines = [f"score {s.match_score:.3f}"]
            if in_um:
                lines.append(f"x {s.template_x * px_x:.2f}  y {s.template_y * px_y:.2f} um")
            else:
                lines.append(f"x {s.template_x:.1f}  y {s.template_y:.1f} px")
            if getattr(s, "spot_calibrated", False):
                dx, dy = s.template_x - s.spot_x, s.template_y - s.spot_y
                if in_um:
                    dxu, dyu = dx * px_x, dy * px_y
                    lines.append(f"to spot {math.hypot(dxu, dyu):.2f} um "
                                 f"(dx {dxu:+.2f}, dy {dyu:+.2f})")
                else:
                    lines.append(f"to spot {math.hypot(dx, dy):.1f} px")
            if getattr(s, "backups_n", 0):
                k = int(getattr(s, "pattern_driver", 0))
                lines.insert(0, "main" if k == 0 else f"backup {k}")
            drawn.append(self._label(p, QPointF(tp.x() - tw / 2, tp.y() + th / 2 + 6),
                                     "\n".join(lines), T.OK, drawn))
        if self._show_spot_info and getattr(s, "spot_calibrated", False):
            sp = self._img_to_widget(s.spot_x, s.spot_y)
            if s.spot_found:
                ref = getattr(cfg.spot, "ref_area", 0.0)
                rel = f"  ({s.spot_area / ref * 100:.0f} %)" if ref > 0 else ""
                area = (f"{s.spot_area * px_x * px_y:.2f} um²" if in_um
                        else f"{s.spot_area:.0f} px²")
                text = f"area {area}{rel}"
            else:
                text = "spot not seen"
            drawn.append(self._label(p, QPointF(sp.x() + 20, sp.y() - 20), text,
                                     SPOT_GREEN, drawn))

    def _label(self, p: QPainter, at: QPointF, text: str, colour: str,
               avoid: list = ()) -> QRectF:
        """Text on a dark translucent box at ``at`` (top-left), kept inside the
        view and moved clear of the boxes in ``avoid``. Returns its rectangle."""
        fm = p.fontMetrics()
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(t) for t in lines) + 10
        h = fm.height() * len(lines) + 6

        def place(x, y):
            return QRectF(min(max(2.0, x), self.width() - w - 2),
                          min(max(2.0, y), self.height() - h - 2), w, h)

        box = place(at.x(), at.y())
        for dy in (0, h + 4, -(h + 4), 2 * (h + 4), -2 * (h + 4)):
            trial = place(at.x(), at.y() + dy)
            if not any(trial.intersects(r) for r in avoid):
                box = trial
                break
        else:                                   # boxed in vertically: go right
            right = max((r.right() for r in avoid), default=at.x())
            box = place(right + 6, at.y())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(box, 4, 4)
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(colour))
        for i, t in enumerate(lines):
            p.drawText(QPointF(box.x() + 5, box.y() + 3 + fm.ascent() + i * fm.height()), t)
        return box
