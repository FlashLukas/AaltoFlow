"""CameraView -- the signature widget for this module (blueprint §7).

Where the magnet has a dipole glyph and the RF gen has a radiating antenna, the
camera module's personality IS its live image with the tracking overlays painted
on top: the laser-spot crosshair, the matched template box, the scanning-point
array, the currently selected point, the pattern 'safety area', and the stage
travel range.  It reads instantly what the feedback system is doing.

It also turns mouse clicks into image-pixel coordinates (letter-box aware) and
emits them, so the window can wire "click to go" and template-ROI selection to
it without the widget needing to know the brain.

ZOOM (2026-09-29, Lukas: "when you call autofocus the image will zoom to the
spot detection area"): ``set_zoom((x0, y0, x1, y1))`` shows only that part of
the frame, scaled to fit. ONE transform (source rectangle + scale + where it
lands in the widget) is used for the picture, every overlay and every
click, so a click on a zoomed view still names the right image pixel.

FREE ZOOM AND PAN (2026-10-02): the mouse wheel zooms about the cursor (the
image point under the cursor stays under it), the MIDDLE button -- or Space
held + the left button -- drags the picture (the left button alone is taken:
click-to-go, template ROI, scan rectangle), ``fit()`` shows the whole frame
and ``one_to_one()`` one camera pixel per screen pixel. All of it only
chooses a new source rectangle for the same transform, so clicks and
overlays stay right. It is display only: nothing is sent to the camera, so
a viewer window can zoom as freely as the one in control.

DRIVEN POINT (2026-10-02): while another client (a scan) picks the scan
point, the window hands the view the point it is moving to and the points
visited so far (``set_drive_marks``); they are drawn on the array points.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

from . import theme as T
from .. import vision as V

# Spot overlays are GREEN with a dark outline (Lukas, 2026-09-13: amber/red were
# hard to read). They sit on a grayscale camera image whatever the GUI theme, so
# these are deliberately theme-independent -- not palette entries.
SPOT_GREEN = "#2bff6a"                 # the spot position (crosshair) + detected box
SPOT_TINT_BGRA = (106, 255, 43, 105)   # thresholded pixels, same green, translucent
OUTLINE = QColor(0, 0, 0, 200)         # under every green line: readable on white too
AIM_COLOUR = "#ff4fd8"                 # the stabiliser's target (selected scan point)
LASER_TARGET_COLOUR = "#35d4ff"        # where the laser is being PLACED (set_laser_target)
AF_POSITION_COLOUR = "#b48cff"         # the AF position (autofocus_at_position): violet,
                                       # unlike every other mark on the image

# Free zoom. One wheel notch multiplies the magnification by WHEEL_STEP, so
# four notches double it (1.19**4 = 2.0): fine enough to stop where you want,
# coarse enough to get from the whole frame to single pixels in a few turns.
WHEEL_STEP = 2.0 ** 0.25
# The deepest zoom, in screen pixels per camera pixel: at 32 one camera pixel
# is a 32 px square -- enough to read single pixel values, and the shown
# part of the frame (a few tens of pixels) never becomes empty.
MAX_ZOOM = 32.0


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
    # a double-click on the image while a zoom NOTE is shown (the autofocus
    # zoom): the window un-zooms for the rest of that run
    unzoom_requested = Signal()
    # the USER changed the zoom (wheel, pan, Fit, 1:1) -- not set_zoom(), which
    # is the window's own (autofocus, spot region). The window uses it to let
    # the user's zoom win over a running autofocus zoom.
    user_zoomed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 360)
        self._img: QImage | None = None
        self._buf = None                    # keep the numpy buffer alive
        self._frame_w = 640
        self._frame_h = 480
        self._draw_rect = QRectF(0, 0, 1, 1)  # where the image is drawn (widget px)
        self._scale = 1.0
        # The part of the frame that is shown, in IMAGE px (x0, y0, x1, y1);
        # None = the whole frame. _src is the same as a QRectF, recomputed by
        # _layout() together with _scale and _draw_rect.
        self._zoom: tuple | None = None
        self._src = QRectF(0, 0, 640, 480)
        self._zoom_note = ""          # text on the view while zoomed by autofocus
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
        # DISPLAY-only contrast stretch (Lukas 2026-09-29): at the short
        # autofocus exposure the background is ~3 grey levels and a defocused
        # spot is dim, so the zoomed view looked black during a run. The data
        # the brain measures is never touched -- only the picture.
        self._stretch = False
        # Free zoom (2026-10-02). _scale_cap: a magnification BELOW "fit" (the
        # frame smaller than the view shown at 1:1); None = fit as usual.
        self._scale_cap: float | None = None
        self._pan_last = None        # widget point of the last pan step (None = no pan)
        self._pan_button = None      # the button that started the pan
        self._space_down = False     # Space held: the left button pans
        # The point an external client is moving to + the ones visited (indices)
        self._drive_target = None
        self._drive_visited: list = []
        self.setMouseTracking(True)
        # keyboard focus by click or wheel, so Space reaches the view
        self.setFocusPolicy(Qt.WheelFocus)

    # -- data in ----------------------------------------------------------- #
    def set_stretch(self, on: bool) -> None:
        """Stretch the displayed contrast (background -> black, the brightest
        pixel -> white) -- used while the autofocus exposure is active."""
        self._stretch = bool(on)

    @staticmethod
    def stretched(gray: np.ndarray) -> np.ndarray:
        """Map [median, max] of the frame onto [0, 255]. The median is the
        background (the spot is a tiny part of the frame); the MAX, not a high
        percentile, so a small spot becomes white. A flat frame (range < 8
        grey levels) is left as it is -- stretching noise shows nothing."""
        lo = float(np.median(gray))
        hi = float(gray.max())
        if hi - lo < 8:
            return gray
        out = (gray.astype(np.float32) - lo) * (255.0 / (hi - lo))
        return np.clip(out, 0, 255).astype(np.uint8)

    def set_frame(self, gray: np.ndarray | None) -> None:
        if gray is None:
            return
        if gray.ndim == 3:
            gray = gray[..., 0]
        gray = np.ascontiguousarray(gray, dtype=np.uint8)
        if self._stretch:
            gray = np.ascontiguousarray(self.stretched(gray))
        self._buf = gray
        self._frame_h, self._frame_w = gray.shape
        self._img = QImage(self._buf.data, self._frame_w, self._frame_h,
                           self._frame_w, QImage.Format_Grayscale8)
        self._layout()                  # a new frame size moves the transform
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

    # -- zoom -------------------------------------------------------------- #
    def set_zoom(self, rect) -> None:
        """Show only ``rect`` = (x0, y0, x1, y1) of the frame (image px), scaled
        to fit with its aspect kept; None = the whole frame.

        The rectangle is clipped to the frame; one that is empty after that
        (or smaller than 2 px) means the whole frame -- never a blank view.
        This is the WINDOW's zoom (autofocus, spot region): it ends a 1:1 view
        of a small frame and does not emit user_zoomed.
        """
        self._apply_zoom(rect, None)

    def _apply_zoom(self, rect, cap) -> None:
        z = None
        if rect is not None:
            x0, y0, x1, y1 = (float(v) for v in rect)
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            x0, y0 = max(0.0, x0), max(0.0, y0)
            x1, y1 = min(float(self._frame_w), x1), min(float(self._frame_h), y1)
            if x1 - x0 >= 2 and y1 - y0 >= 2:
                z = (x0, y0, x1, y1)
        cap = None if z is not None or cap is None else float(cap)
        if z != self._zoom or cap != self._scale_cap:
            self._zoom = z
            self._scale_cap = cap
            self._layout()
            self.update()

    def zoom(self):
        """The zoom rectangle (x0, y0, x1, y1) in image px, or None (whole frame)."""
        return self._zoom

    def set_zoom_note(self, text: str) -> None:
        """A short note drawn on the view (the autofocus zoom says how to leave
        it). Empty = none. While a note is shown a single click is NOT sent
        as click-to-go: it may be the first half of the double-click that
        un-zooms, and a stage move in the middle of an autofocus would spoil it."""
        text = str(text or "")
        if text != self._zoom_note:
            self._zoom_note = text
            self.update()

    def zoom_note(self) -> str:
        return self._zoom_note

    # -- free zoom (wheel, pan, Fit, 1:1) ------------------------------------ #
    def _dpr(self) -> float:
        """Device pixels per widget pixel (Windows display scaling: 1.5 at
        150 %). "1:1 pixels" means one camera pixel per DEVICE pixel."""
        try:
            return float(self.devicePixelRatioF()) or 1.0
        except Exception:
            return 1.0

    def zoom_level(self) -> float:
        """Screen (device) pixels per camera pixel: 1.0 = 1:1, 2.0 = each
        camera pixel is a 2 x 2 square."""
        self._layout()
        return self._scale * self._dpr()

    def zoom_text(self) -> str:
        """What the view shows in its corner: "fit 62 %" or "250 %"."""
        pct = f"{self.zoom_level() * 100:.0f} %"
        return f"fit {pct}" if self._zoom is None and self._scale_cap is None else pct

    def _fit_scale(self) -> float:
        return min(max(self.width(), 1) / max(self._frame_w, 1),
                   max(self.height(), 1) / max(self._frame_h, 1))

    def _view_at(self, cx: float, cy: float, scale: float):
        """(rect, cap) for showing the frame at ``scale`` widget px per camera px
        with (cx, cy) in the middle of the view.

        The rectangle has the WIDGET's shape, so the zoomed picture fills the
        view without bars, and it is slid back inside the frame near an edge
        (rather than showing empty space beyond the camera's picture). In a
        direction where the whole frame fits it simply spans the frame. When
        the whole frame fits both ways there is no rectangle: None, plus a
        cap if the scale is below "fit" (a small frame at 1:1)."""
        fw, fh = float(self._frame_w), float(self._frame_h)
        vw, vh = max(self.width(), 1) / scale, max(self.height(), 1) / scale
        if vw >= fw and vh >= fh:
            return None, (scale if scale < self._fit_scale() * (1 - 1e-9) else None)
        vw, vh = min(vw, fw), min(vh, fh)
        x0 = min(max(cx - vw / 2.0, 0.0), fw - vw)
        y0 = min(max(cy - vh / 2.0, 0.0), fh - vh)
        return (x0, y0, x0 + vw, y0 + vh), None

    def _user_view(self, rect, cap) -> None:
        self._apply_zoom(rect, cap)
        self.user_zoomed.emit()

    def zoom_about(self, wx: float, wy: float, factor: float) -> None:
        """Magnify by ``factor`` keeping the camera pixel under widget point
        (wx, wy) where it is -- the wheel's zoom."""
        self._layout()
        dpr = self._dpr()
        lo = min(self._fit_scale(), 1.0 / dpr)    # whole frame, or 1:1 if smaller
        hi = MAX_ZOOM / dpr
        new = min(max(self._scale * factor, lo), hi)
        if abs(new - self._scale) < 1e-12 * max(new, 1.0):
            return                                  # at a limit already
        ix, iy = self._widget_to_img(wx, wy)
        if abs(new - self._fit_scale()) < 1e-9 * new:
            self._user_view(None, None)             # exactly fit = the plain whole frame
            return
        # the camera pixel (ix, iy) must stay at widget (wx, wy): the view's
        # left edge is ix - wx / new, its centre half a view further
        cx = ix - wx / new + self.width() / (2.0 * new)
        cy = iy - wy / new + self.height() / (2.0 * new)
        self._user_view(*self._view_at(cx, cy, new))

    def pan_by(self, dx: float, dy: float) -> None:
        """Move the picture by (dx, dy) widget px (the drag), stopping at the
        frame's edges. Nothing to pan when the whole frame is shown."""
        if self._zoom is None:
            return
        self._layout()
        x0, y0, x1, y1 = self._zoom
        w, h = x1 - x0, y1 - y0
        nx0 = min(max(x0 - dx / self._scale, 0.0), self._frame_w - w)
        ny0 = min(max(y0 - dy / self._scale, 0.0), self._frame_h - h)
        self._user_view((nx0, ny0, nx0 + w, ny0 + h), None)

    def fit(self) -> None:
        """The whole frame, as large as the view allows."""
        self._user_view(None, None)

    def one_to_one(self) -> None:
        """One camera pixel per screen pixel, centred on the middle of what is
        shown now (so 1:1 looks closer at the same place)."""
        self._layout()
        cx = self._src.left() + self._src.width() / 2.0
        cy = self._src.top() + self._src.height() / 2.0
        self._user_view(*self._view_at(cx, cy, 1.0 / self._dpr()))

    # -- the point an external client drives (2026-10-02) -------------------- #
    def set_drive_marks(self, target, visited=()) -> None:
        """``target`` = (ix, iy) of the array point a client is moving to (None:
        none / settled); ``visited`` = the points it went to before."""
        target = None if target is None else (int(target[0]), int(target[1]))
        visited = [(int(a), int(b)) for a, b in visited]
        if (target, visited) != (self._drive_target, self._drive_visited):
            self._drive_target, self._drive_visited = target, visited
            self.update()

    def drive_marks(self) -> tuple:
        return (self._drive_target, list(self._drive_visited))

    # -- coordinate mapping ------------------------------------------------ #
    def _layout(self) -> None:
        """Recompute the ONE image->widget transform: the shown source
        rectangle (zoom or whole frame) fitted into the widget, aspect kept,
        centred (letter-box). Everything -- picture, overlays, clicks -- uses it."""
        if self._zoom is not None:
            x0, y0, x1, y1 = self._zoom
        else:
            x0, y0, x1, y1 = 0.0, 0.0, float(self._frame_w), float(self._frame_h)
        sw, sh = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        w, h = max(self.width(), 1), max(self.height(), 1)
        self._src = QRectF(x0, y0, sw, sh)
        self._scale = min(w / sw, h / sh)
        if self._zoom is None and self._scale_cap is not None:
            self._scale = min(self._scale, self._scale_cap)   # small frame at 1:1
        dw, dh = sw * self._scale, sh * self._scale
        self._draw_rect = QRectF((w - dw) / 2.0, (h - dh) / 2.0, dw, dh)

    def resizeEvent(self, ev):
        self._layout()
        super().resizeEvent(ev)

    def _img_to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self._draw_rect.left() + (x - self._src.left()) * self._scale,
                       self._draw_rect.top() + (y - self._src.top()) * self._scale)

    def _widget_to_img(self, x: float, y: float) -> tuple:
        ix = self._src.left() + (x - self._draw_rect.left()) / self._scale
        iy = self._src.top() + (y - self._draw_rect.top()) / self._scale
        return (ix, iy)

    # public names (tests, the window): the same transform as the painter
    def image_to_widget(self, x: float, y: float) -> tuple:
        self._layout()
        return self._img_to_widget(x, y).toTuple()

    def widget_to_image(self, x: float, y: float) -> tuple:
        self._layout()
        return self._widget_to_img(x, y)

    # -- mouse ------------------------------------------------------------- #
    def wheelEvent(self, ev):
        # One notch = 120 units; a touchpad sends fractions -- the power keeps
        # the zoom smooth and the same overall for the same finger travel.
        notches = ev.angleDelta().y() / 120.0
        if notches == 0:
            return
        pos = ev.position()
        self.zoom_about(pos.x(), pos.y(), WHEEL_STEP ** notches)
        ev.accept()

    def _pan_starts(self, ev) -> bool:
        return (ev.button() == Qt.MiddleButton
                or (ev.button() == Qt.LeftButton and self._space_down))

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Space:
            if not ev.isAutoRepeat():
                self._space_down = True
                if self._pan_last is None:
                    self.setCursor(Qt.OpenHandCursor)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if ev.key() == Qt.Key_Space:
            if not ev.isAutoRepeat():
                self._space_down = False
                if self._pan_last is None:
                    self.unsetCursor()
            ev.accept()
            return
        super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        # a Space released while another widget had the keyboard never arrives here
        self._space_down = False
        if self._pan_last is None:
            self.unsetCursor()
        super().focusOutEvent(ev)

    def enterEvent(self, ev):
        # The mouse over the picture: take the keyboard focus so Space pans --
        # otherwise Space would PRESS whichever button had focus (Snapshot,
        # Select...). Never from a box being typed into.
        fw = QApplication.focusWidget()
        if fw is None or isinstance(fw, QAbstractButton):
            self.setFocus(Qt.MouseFocusReason)
        super().enterEvent(ev)

    def mousePressEvent(self, ev):
        if self._pan_starts(ev):
            # a pan: the left button's own jobs (click-to-go, ROI, scan
            # rectangle) do not happen while Space is held
            self._pan_last = ev.position()
            self._pan_button = ev.button()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() != Qt.LeftButton:
            return
        ipt = self._widget_to_img(ev.position().x(), ev.position().y())
        if self._zoom_note and not (self._scan_mode or self._roi_mode):
            return            # see set_zoom_note: maybe half of a double-click
        if self._scan_mode:
            self._scan_press(ipt)
        elif self._roi_mode:
            self._drag_start = ev.position()
            self._drag_now = ev.position()
        else:
            if 0 <= ipt[0] < self._frame_w and 0 <= ipt[1] < self._frame_h:
                self.clicked.emit(ipt[0], ipt[1])

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._zoom_note:
            self.unzoom_requested.emit()
            return
        super().mouseDoubleClickEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._pan_last is not None:
            pos = ev.position()
            self.pan_by(pos.x() - self._pan_last.x(), pos.y() - self._pan_last.y())
            self._pan_last = pos
            return
        if self._scan_mode and self._scan_kind is not None:
            self._scan_move(self._widget_to_img(ev.position().x(), ev.position().y()))
        elif self._roi_mode and self._drag_start is not None:
            self._drag_now = ev.position()
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._pan_last is not None:
            if ev.button() == self._pan_button:
                self._pan_last = self._pan_button = None
                if self._space_down:
                    self.setCursor(Qt.OpenHandCursor)
                else:
                    self.unsetCursor()
            return
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

        # Fit the SHOWN part of the frame (zoom or all of it) into the widget,
        # preserving aspect ratio (letter-box). _layout is the one transform.
        self._layout()

        if self._img is not None:
            p.drawImage(self._draw_rect, self._img, self._src)
            if self._show_threshold:
                self._paint_threshold(p)
        else:
            p.setPen(QColor(T.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "no frame")

        p.setRenderHint(QPainter.Antialiasing, True)
        # Zoomed, overlays of things outside the shown part would be drawn on
        # the letter-box bars as if they were in the picture: clip them.
        if self._zoom is not None:
            p.save()
            p.setClipRect(self._draw_rect)
        self._paint_overlays(p)
        self._paint_scan_rect(p)
        if self._zoom is not None:
            p.restore()
        if self._zoom_note:
            self._label(p, QPointF(self._draw_rect.left() + 6, self._draw_rect.top() + 6),
                        self._zoom_note, T.COLORS["accent_hi"])
        if self._img is not None:
            # the zoom level in the picture's bottom-right corner
            text = self.zoom_text()
            fm = p.fontMetrics()
            w, h = fm.horizontalAdvance(text) + 10, fm.height() + 6
            self._label(p, QPointF(self._draw_rect.right() - w - 4,
                                   self._draw_rect.bottom() - h - 4), text,
                        T.COLORS["accent"])

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
        p.drawImage(self._draw_rect, img, self._src)

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
            offs, acx, acy = self._array_geometry()
            six = int(np.clip(cfg.scanning.selected_index_x, 0, cfg.scanning.points_x - 1))
            siy = int(np.clip(cfg.scanning.selected_index_y, 0, cfg.scanning.points_y - 1))
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
            self._paint_drive_marks(p)

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

        # -- where the SIZE is measured, when it is LOCATED (2026-09-29) ------ #
        # Spot.locate = peak / blob: the found centre as a small amber SQUARE
        # (not the calibrated green "+", the stabiliser's "x" or the laser
        # target's diamond) and, dotted, the box the size was integrated over.
        # Information only -- motion uses the "+".
        fx, fy = getattr(s, "spot_found_x", float("nan")), getattr(s, "spot_found_y", float("nan"))
        if (getattr(cfg.spot, "locate", "calibrated") != "calibrated"
                and math.isfinite(fx) and math.isfinite(fy)):
            c = self._img_to_widget(fx, fy)
            d = 7.0
            p.setBrush(Qt.NoBrush)
            for _ in outlined_pen(p, T.COLORS["accent"], 2.0):
                p.drawRect(QRectF(c.x() - d, c.y() - d, 2 * d, 2 * d))
            box = tuple(getattr(s, "spot_size_box", (0, 0, 0, 0)) or (0, 0, 0, 0))
            if len(box) == 4 and box[2] > box[0] and box[3] > box[1]:
                a = self._img_to_widget(box[0] - 0.5, box[1] - 0.5)
                b = self._img_to_widget(box[2] - 0.5, box[3] - 0.5)
                for _ in outlined_pen(p, T.COLORS["accent"], 1.0, Qt.DotLine):
                    p.drawRect(QRectF(a, b))

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

        # -- where the laser is being PLACED (Laser on sample card) ------------ #
        # A diamond at the target point of the sample, drawn while a target is
        # set and the pattern is matched (the target hangs off the pattern, so
        # it moves with the sample); a line from the spot while it is still
        # being placed. Cyan, so it never reads as the stabiliser's pink "x".
        tx = getattr(s, "laser_target_x_um", float("nan"))
        ty = getattr(s, "laser_target_y_um", float("nan"))
        pxx, pxy = getattr(s, "pixel_size_x", 0.0), getattr(s, "pixel_size_y", 0.0)
        if (getattr(s, "match_found", False) and tx == tx and ty == ty
                and pxx > 0 and pxy > 0):
            tp = self._img_to_widget(s.anchor_x + tx / pxx, s.anchor_y + ty / pxy)
            d = 8
            diamond = QPolygonF([QPointF(tp.x(), tp.y() - d), QPointF(tp.x() + d, tp.y()),
                                 QPointF(tp.x(), tp.y() + d), QPointF(tp.x() - d, tp.y())])
            for _ in outlined_pen(p, LASER_TARGET_COLOUR, 2.0):
                p.drawPolygon(diamond)
                if getattr(s, "laser_goto", False) and getattr(s, "spot_calibrated", False):
                    p.drawLine(self._img_to_widget(s.spot_x, s.spot_y), tp)

        # -- the AF POSITION (where autofocus_at_position finds focus) -------- #
        # A violet square labelled "AF", drawn while one is set and the
        # pattern is matched (an array point or um from the main template:
        # both hang off the pattern, so the mark moves with the sample). It
        # may be off-screen -- then simply not seen.
        self._paint_af_position(p, s, cfg)

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
                short = getattr(s, "spot_found_why_short", "")
                if "paused" in short:
                    # the frame is at the autofocus exposure: the fixed threshold
                    # was not applied, so "not seen" would be false (rig 2026-09-29)
                    text = short
                else:
                    text = f"spot not seen: {short}" if short else "spot not seen"
            drawn.append(self._label(p, QPointF(sp.x() + 20, sp.y() - 20), text,
                                     SPOT_GREEN, drawn))

    def _array_geometry(self):
        """(offsets, centre x, centre y) of the scan-point array in image px,
        or None without a matched pattern. The brain reports only the
        SELECTED point's position; the array centre is that point minus its
        own offset, and every other point is centre + its offset."""
        s, cfg = self._status, self._cfg
        if s is None or cfg is None or not getattr(s, "match_found", False):
            return None
        sc = cfg.scanning
        offs = V.scanning_array_pixel_offsets(
            sc.points_x, sc.points_y, sc.dx_um, sc.dy_um, sc.angle_deg,
            cfg.image.pixel_size_x_um, cfg.image.pixel_size_y_um)
        six = int(np.clip(sc.selected_index_x, 0, sc.points_x - 1))
        siy = int(np.clip(sc.selected_index_y, 0, sc.points_y - 1))
        return (offs, s.selected_point_x - offs[siy, six, 0],
                s.selected_point_y - offs[siy, six, 1])

    def af_position_image(self):
        """Image position (x, y) of the configured AF position, or None (none
        set, no matched pattern, an index outside the array)."""
        s, cfg = self._status, self._cfg
        if s is None or cfg is None or not getattr(s, "match_found", False):
            return None
        sc = cfg.scanning
        if not getattr(sc, "af_position_set", False):
            return None
        if getattr(sc, "af_position", "index") == "index":
            return self.array_point_image(int(sc.af_index_x), int(sc.af_index_y))
        pxx, pxy = getattr(s, "pixel_size_x", 0.0), getattr(s, "pixel_size_y", 0.0)
        if pxx <= 0 or pxy <= 0:
            return None
        return (s.anchor_x + float(sc.af_x_um) / pxx, s.anchor_y + float(sc.af_y_um) / pxy)

    def _paint_af_position(self, p: QPainter, s, cfg) -> None:
        at = self.af_position_image()
        if at is None:
            return
        c = self._img_to_widget(*at)
        d = 7
        p.save()                                        # the bold font stays in here
        p.setBrush(Qt.NoBrush)
        for _ in outlined_pen(p, AF_POSITION_COLOUR, 2.0):
            p.drawRect(QRectF(c.x() - d, c.y() - d, 2 * d, 2 * d))
        f = p.font(); f.setBold(True); p.setFont(f)
        at_txt = QPointF(c.x() + d + 3, c.y() - d)
        p.setPen(QPen(OUTLINE))                         # a dark shadow: readable on white
        p.drawText(at_txt + QPointF(1, 1), "AF")
        p.setPen(QPen(QColor(AF_POSITION_COLOUR)))
        p.drawText(at_txt, "AF")
        p.restore()

    def array_point_image(self, ix: int, iy: int):
        """Image position (x, y) of array point (ix, iy), or None (no match,
        or an index outside the array)."""
        g = self._array_geometry()
        if g is None:
            return None
        offs, acx, acy = g
        if not (0 <= iy < offs.shape[0] and 0 <= ix < offs.shape[1]):
            return None
        return (float(acx + offs[iy, ix, 0]), float(acy + offs[iy, ix, 1]))

    def _paint_drive_marks(self, p: QPainter) -> None:
        """The points an external client (a scan) went to: small pink dots;
        the point it is moving to now: a pink target ring with four ticks.
        Pink = the stabiliser's aim colour, because that IS where the stage
        is being driven; a ring (not an "x") says "on its way there"."""
        for ix, iy in self._drive_visited:
            pt = self.array_point_image(ix, iy)
            if pt is None:
                continue
            c = self._img_to_widget(*pt)
            p.setPen(QPen(OUTLINE, 1.0))
            p.setBrush(QColor(AIM_COLOUR))
            p.drawEllipse(c, 2.5, 2.5)
        p.setBrush(Qt.NoBrush)
        if self._drive_target is None:
            return
        pt = self.array_point_image(*self._drive_target)
        if pt is None:
            return
        c = self._img_to_widget(*pt)
        r, t = 11.0, 6.0
        for _ in outlined_pen(p, AIM_COLOUR, 2.0):
            p.drawEllipse(c, r, r)
            p.drawLine(QPointF(c.x() - r - t, c.y()), QPointF(c.x() - r + 2, c.y()))
            p.drawLine(QPointF(c.x() + r - 2, c.y()), QPointF(c.x() + r + t, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - r - t), QPointF(c.x(), c.y() - r + 2))
            p.drawLine(QPointF(c.x(), c.y() + r - 2), QPointF(c.x(), c.y() + r + t))

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
