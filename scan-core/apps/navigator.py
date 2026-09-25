"""Navigator tab: find your way around a large sample on its design file.

    1. Open the design: a GDS/OASIS file (drawn as vectors, exact um), or an
       image whose real width you type in.
    2. Tell it where you are: put a feature under the laser (camera), click the
       same feature on the design, press "I am here". One point uses the
       rotation you set by eye; a second point FITS the rotation (and the
       stage's scale); from three on the residuals say how good it is.
    3. Click anywhere: the stage coordinates are shown, "Go there" moves.
    4. On an open-loop stage (KIM) the position drifts: click the feature you
       actually see and press "correct offset" -- one click, rotation kept.

The maths is in scan_core/navigator.py (no Qt, unit-tested). This file only
draws and forwards clicks.

The picture is drawn in STAGE coordinates (um, y up): the design is placed
onto the stage frame by the current registration, so the travel limits, the
stage crosshair and the camera field of view are drawn as they are, and a
click is already a stage position. Mouse: click = select, drag = pan, wheel =
zoom around the cursor.

It moves the stage through the same Settables a scan uses (the module's own
`describe`), so it works with KIM (um) and the BSC203 (mm) alike, and with the
simulator when nothing is connected.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from apps.theme import C
from scan_core.lab import module_prefix
from scan_core.navigator import (
    Design, Registration, image_design, load_gds, load_session, save_session,
    stage_pairs, waypoints,
)

# Layer fill colours. Data colours, not theme colours: a layer must keep its
# colour when the theme changes, like a trace in a plot.
LAYER_COLOURS = ["#4e9cff", "#ff9e2c", "#3ddc84", "#e05ad6", "#f2d024",
                 "#29c7c7", "#ff5c5c", "#a58bff", "#8fd14f", "#ff8fa3"]
POLL_MS = 200


def _qtransform(M, t) -> QtGui.QTransform:
    # QTransform's (m11, m12, m21, m22) is the TRANSPOSE of the usual matrix:
    # x' = m11 x + m21 y + dx,  y' = m12 x + m22 y + dy.
    return QtGui.QTransform(M[0, 0], M[1, 0], M[0, 1], M[1, 1], t[0], t[1])


def _fmt_um(v: float) -> str:
    return f"{v:,.1f}".replace(",", " ")


# --------------------------------------------------------------------------- #
# markers drawn at a fixed SCREEN size, whatever the zoom
# --------------------------------------------------------------------------- #
class _Marker(QtWidgets.QGraphicsItem):
    """A crosshair / target / reference mark that does not scale with zoom.

    ItemIgnoresTransformations also ignores the view's y flip, so the number
    of a reference point is drawn upright.
    """

    def __init__(self, kind: str, text: str = ""):
        super().__init__()
        self.kind, self.text = kind, text
        self.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations)
        self.setZValue({"stage": 30, "target": 40, "ref": 35}[kind])

    def boundingRect(self):
        return QtCore.QRectF(-16, -16, 60, 32)

    def paint(self, p, _opt, _w=None):
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        if self.kind == "stage":
            pen = QtGui.QPen(QtGui.QColor(C["ok"]), 2)
            p.setPen(pen)
            p.drawLine(-14, 0, -4, 0); p.drawLine(4, 0, 14, 0)
            p.drawLine(0, -14, 0, -4); p.drawLine(0, 4, 0, 14)
            p.drawEllipse(QtCore.QPointF(0, 0), 2, 2)
        elif self.kind == "target":
            pen = QtGui.QPen(QtGui.QColor(C["accent"]), 2)
            p.setPen(pen)
            p.drawEllipse(QtCore.QPointF(0, 0), 9, 9)
            p.drawLine(-14, 0, 14, 0); p.drawLine(0, -14, 0, 14)
        else:
            col = QtGui.QColor(C["accent_hi"])
            p.setPen(QtGui.QPen(col, 1.5))
            p.setBrush(QtGui.QColor(C["bg"]))
            p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(0, -7), QtCore.QPointF(7, 0),
                                           QtCore.QPointF(0, 7), QtCore.QPointF(-7, 0)]))
            p.setPen(col)
            f = p.font(); f.setBold(True); p.setFont(f)
            p.drawText(QtCore.QPointF(10, 5), self.text)


# --------------------------------------------------------------------------- #
# the canvas
# --------------------------------------------------------------------------- #
class NavCanvas(QtWidgets.QGraphicsView):
    """Scene = stage um, y up. Click selects, drag pans, wheel zooms."""

    clicked = QtCore.Signal(float, float)       # stage um
    hovered = QtCore.Signal(float, float)

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.scale(1, -1)                        # y up, like the GDS and the stage
        self.setBackgroundBrush(QtGui.QColor(C["code_bg"]))
        # Panning moves the view over the scene rect, so make it far larger
        # than any stage (+-1 m) -- otherwise you cannot pan past the design.
        scene.setSceneRect(-1e6, -1e6, 2e6, 2e6)
        self._press = None
        self._panning = False

    def wheelEvent(self, ev):
        f = 1.25 if ev.angleDelta().y() > 0 else 1 / 1.25
        self.scale(f, f)

    def mousePressEvent(self, ev):
        if ev.button() in (QtCore.Qt.LeftButton, QtCore.Qt.MiddleButton, QtCore.Qt.RightButton):
            self._press = ev.position()
            self._panning = ev.button() != QtCore.Qt.LeftButton
            ev.accept()

    def mouseMoveEvent(self, ev):
        s = self.mapToScene(ev.position().toPoint())
        self.hovered.emit(s.x(), s.y())
        if self._press is None:
            return
        d = ev.position() - self._press
        # A few pixels of hand shake is still a click, not a drag.
        if not self._panning and abs(d.x()) + abs(d.y()) < 5:
            return
        self._panning = True
        self._press = ev.position()
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        h.setValue(h.value() - int(d.x())); v.setValue(v.value() - int(d.y()))

    def mouseReleaseEvent(self, ev):
        if self._press is not None and not self._panning and ev.button() == QtCore.Qt.LeftButton:
            s = self.mapToScene(ev.position().toPoint())
            self.clicked.emit(s.x(), s.y())
        self._press = None
        self._panning = False

    def fit(self, rect: QtCore.QRectF) -> None:
        if rect.isEmpty():
            return
        m = 0.05 * max(rect.width(), rect.height())
        self.fitInView(rect.adjusted(-m, -m, m, m), QtCore.Qt.KeepAspectRatio)


class _Bridge(QtCore.QObject):
    """Brings 'the move finished' from the move thread to the GUI thread."""
    done = QtCore.Signal(str)


# --------------------------------------------------------------------------- #
# the tab
# --------------------------------------------------------------------------- #
class NavigatorWidget(QtWidgets.QWidget):
    def __init__(self, on_log=None, is_busy=None, parent=None):
        super().__init__(parent)
        self.on_log = on_log or (lambda msg: None)
        # The suite passes "is a scan running?": the navigator must not move a
        # stage that a scan is driving.
        self.is_busy = is_busy or (lambda: False)
        self.registry = None
        self.lab = None
        self.pairs = []
        self.design: Design | None = None
        self.reg = Registration()
        self.pick = None                 # selected point, DESIGN um
        self._moving = False
        self._stop = threading.Event()
        self._bridge = _Bridge()
        self._bridge.done.connect(self._move_finished)
        self._pos_um = None              # last read stage position

        self.scene = QtWidgets.QGraphicsScene(self)
        self.canvas = NavCanvas(self.scene)
        self.canvas.clicked.connect(self._on_click)
        self.canvas.hovered.connect(self._on_hover)
        self._design_group = QtWidgets.QGraphicsItemGroup()
        self._design_group.setZValue(0)
        self.scene.addItem(self._design_group)
        self._layer_items: dict[str, QtWidgets.QGraphicsPathItem] = {}

        dash = QtGui.QPen(QtGui.QColor(C["muted"]), 1, QtCore.Qt.DashLine); dash.setCosmetic(True)
        self._limits_item = self.scene.addRect(QtCore.QRectF(), dash)
        self._limits_item.setZValue(5)
        fov = QtGui.QPen(QtGui.QColor(C["ok"]), 1.5); fov.setCosmetic(True)
        self._fov_item = self.scene.addRect(QtCore.QRectF(), fov)
        self._fov_item.setZValue(25)
        self._stage_mark = _Marker("stage"); self.scene.addItem(self._stage_mark)
        self._stage_mark.hide()
        self._target_mark = _Marker("target"); self.scene.addItem(self._target_mark)
        self._target_mark.hide()
        self._ref_marks: list[_Marker] = []

        self._build_ui()
        self._refresh_all()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)

    # ---------------------------------------------------------------- UI --
    def _card(self, title):
        card = QtWidgets.QFrame(); card.setObjectName("card")
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 12); v.setSpacing(6)
        t = QtWidgets.QLabel(title); t.setObjectName("tag"); v.addWidget(t)
        return card, v

    def _muted(self, text=""):
        lbl = QtWidgets.QLabel(text); lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{C['muted']};")
        return lbl

    def _build_ui(self):
        side = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(side); sv.setContentsMargins(0, 0, 6, 0); sv.setSpacing(10)

        # --- design
        card, v = self._card("DESIGN")
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Open GDS..."); b.clicked.connect(self._open_gds_dialog)
        row.addWidget(b)
        b = QtWidgets.QPushButton("Open image..."); b.clicked.connect(self._open_image_dialog)
        row.addWidget(b)
        v.addLayout(row)
        self.design_lbl = self._muted("No design loaded.")
        v.addWidget(self.design_lbl)
        wrow = QtWidgets.QHBoxLayout()
        self.width_lbl = QtWidgets.QLabel("real width")
        self.width_spin = QtWidgets.QDoubleSpinBox()
        self.width_spin.setRange(0.1, 1e6); self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" um"); self.width_spin.setKeyboardTracking(False)
        self.width_spin.setToolTip("The physical width of the whole image. Changing it "
                                   "changes the design's scale, so the reference points "
                                   "are cleared.")
        self.width_spin.valueChanged.connect(self._width_changed)
        wrow.addWidget(self.width_lbl); wrow.addWidget(self.width_spin, 1)
        v.addLayout(wrow)
        self.layer_list = QtWidgets.QListWidget()
        self.layer_list.setMaximumHeight(96)
        self.layer_list.itemChanged.connect(self._layer_toggled)
        v.addWidget(self.layer_list)
        sv.addWidget(card)

        # --- stage
        card, v = self._card("STAGE")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("move with"))
        self.stage_combo = QtWidgets.QComboBox()
        self.stage_combo.currentIndexChanged.connect(lambda _i: self._refresh_all())
        row.addWidget(self.stage_combo, 1)
        v.addLayout(row)
        self.pos_lbl = QtWidgets.QLabel("-")
        self.pos_lbl.setStyleSheet(f"color:{C['ok']}; font-weight:700;")
        v.addWidget(self.pos_lbl)
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(8)
        self.fov_w = self._um_spin(0, 1e5, 0, "Width of the camera's field of view, drawn "
                                    "around the stage position. 0 hides it.")
        self.fov_h = self._um_spin(0, 1e5, 0, "Height of the camera's field of view.")
        grid.addWidget(QtWidgets.QLabel("camera view"), 0, 0)
        grid.addWidget(self.fov_w, 0, 1); grid.addWidget(QtWidgets.QLabel("x"), 0, 2)
        grid.addWidget(self.fov_h, 0, 3)
        self.approach = self._um_spin(0, 1e4, 0,
            "Arrive from ONE side: the last part of every move runs in +X and +Y over "
            "this distance. An open-loop inertia stage (KIM) steps differently in each "
            "direction, so this makes a position repeatable. 0 = straight there.")
        grid.addWidget(QtWidgets.QLabel("final approach"), 1, 0)
        grid.addWidget(self.approach, 1, 1, 1, 3)
        v.addLayout(grid)
        for s in (self.fov_w, self.fov_h):
            s.valueChanged.connect(lambda _v: self._update_stage_marks())
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Fit design"); b.clicked.connect(self.fit_design); row.addWidget(b)
        b = QtWidgets.QPushButton("Centre on stage"); b.clicked.connect(self._centre_on_stage)
        row.addWidget(b)
        v.addLayout(row)
        sv.addWidget(card)

        # --- selected point
        card, v = self._card("SELECTED POINT")
        self.pick_lbl = QtWidgets.QLabel("Click on the design.")
        self.pick_lbl.setWordWrap(True)
        self.pick_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.pick_lbl)
        row = QtWidgets.QHBoxLayout()
        self.go_btn = QtWidgets.QPushButton("Go there"); self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.go)
        self.stop_btn = QtWidgets.QPushButton("Stop"); self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.go_btn, 1); row.addWidget(self.stop_btn)
        v.addLayout(row)
        self.here_btn = QtWidgets.QPushButton("I am here: add reference point")
        self.here_btn.setToolTip(
            "The selected design point is under the laser now. Adds a reference point "
            "and refits: 1 point = offset, 2 = rotation + scale, 3+ = with residuals.")
        self.here_btn.clicked.connect(self.add_reference)
        v.addWidget(self.here_btn)
        self.offset_btn = QtWidgets.QPushButton("I am here: correct offset only")
        self.offset_btn.setToolTip(
            "For drift (open-loop KIM): shift the whole mapping so the selected point "
            "is where the stage is now. Rotation and scale are kept.")
        self.offset_btn.clicked.connect(self.correct_offset)
        v.addWidget(self.offset_btn)
        sv.addWidget(card)

        # --- registration
        card, v = self._card("REGISTRATION")
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(8)
        self.rot_spin = QtWidgets.QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360); self.rot_spin.setDecimals(2)
        self.rot_spin.setSuffix(" deg"); self.rot_spin.setSingleStep(1.0)
        self.rot_spin.setWrapping(True); self.rot_spin.setKeyboardTracking(False)
        self.rot_spin.setToolTip("Angle of the design on the stage, set by eye. Used "
                                 "until there are two reference points; then fitted.")
        self.rot_spin.valueChanged.connect(self._prior_changed)
        self.mirror_box = QtWidgets.QCheckBox("mirrored")
        self.mirror_box.setToolTip("Sample face down, or a stage axis runs the other way. "
                                   "Detected by itself from three reference points on.")
        self.mirror_box.clicked.connect(self._prior_changed)
        grid.addWidget(QtWidgets.QLabel("rotation"), 0, 0)
        grid.addWidget(self.rot_spin, 0, 1); grid.addWidget(self.mirror_box, 0, 2)
        rot_row = QtWidgets.QHBoxLayout()
        for d in (-90, -1, 1, 90):
            b = QtWidgets.QPushButton(f"{d:+d}"); b.setFixedWidth(44)
            b.clicked.connect(lambda _c=False, d=d: self.rot_spin.setValue(self.rot_spin.value() + d))
            rot_row.addWidget(b)
        rot_row.addStretch(1)
        grid.addLayout(rot_row, 1, 1, 1, 2)
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems(["auto", "similarity", "affine"])
        self.model_combo.setToolTip(
            "auto: 2-3 points = rotation + one scale, 4+ = affine (separate X/Y scale "
            "and skew, e.g. KIM X and Y step sizes that disagree).")
        self.model_combo.currentTextChanged.connect(self._model_changed)
        grid.addWidget(QtWidgets.QLabel("fit"), 2, 0); grid.addWidget(self.model_combo, 2, 1)
        v.addLayout(grid)
        self.points_table = QtWidgets.QTableWidget(0, 3)
        self.points_table.setHorizontalHeaderLabels(["#", "design (um)", "error (um)"])
        self.points_table.verticalHeader().setVisible(False)
        self.points_table.horizontalHeader().setStretchLastSection(True)
        self.points_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.points_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.points_table.setMaximumHeight(130)
        v.addWidget(self.points_table)
        self.fit_lbl = self._muted()
        v.addWidget(self.fit_lbl)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Remove point"); b.clicked.connect(self._remove_point)
        row.addWidget(b)
        b = QtWidgets.QPushButton("Clear all"); b.clicked.connect(self._clear_points)
        row.addWidget(b)
        v.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Save session..."); b.clicked.connect(self._save_dialog)
        row.addWidget(b)
        b = QtWidgets.QPushButton("Load session..."); b.clicked.connect(self._load_dialog)
        row.addWidget(b)
        v.addLayout(row)
        sv.addWidget(card)
        sv.addStretch(1)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setWidget(side); scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # Vertical scrolling only: a sideways scrollbar under a sidebar just
        # hides the last row of buttons behind itself.
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(side.minimumSizeHint().width() + 24)
        scroll.setMaximumWidth(440)

        right = QtWidgets.QVBoxLayout(); right.setSpacing(4)
        right.addWidget(self.canvas, 1)
        self.hover_lbl = self._muted(" ")
        right.addWidget(self.hover_lbl)

        lay = QtWidgets.QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)
        lay.addLayout(right, 1)

    def _um_spin(self, lo, hi, val, tip):
        s = QtWidgets.QDoubleSpinBox(); s.setRange(lo, hi); s.setValue(val)
        s.setDecimals(1); s.setSuffix(" um"); s.setToolTip(tip)
        return s

    # ------------------------------------------------------------ source --
    def set_source(self, registry=None, lab=None) -> None:
        """The suite hands over its registry whenever it (re)connects."""
        self.registry, self.lab = registry, lab
        keep = self.stage_combo.currentText()
        self.pairs = stage_pairs(registry) if registry is not None else []
        self.stage_combo.blockSignals(True)
        self.stage_combo.clear()
        for p in self.pairs:
            self.stage_combo.addItem(p.label)
        i = self.stage_combo.findText(keep)
        self.stage_combo.setCurrentIndex(i if i >= 0 else 0)
        self.stage_combo.blockSignals(False)
        for p in self.pairs:
            if not p.unit_known:
                self.on_log(f"navigator: {p.label} reports unit {p.x.unit!r}; assuming um")
        self._pos_um = None
        self._refresh_all()

    @property
    def pair(self):
        i = self.stage_combo.currentIndex()
        return self.pairs[i] if 0 <= i < len(self.pairs) else None

    def stage_um(self):
        """Current stage position in um, or None if it cannot be read."""
        p = self.pair
        if p is None:
            return None
        try:
            x, y = float(p.x.get()), float(p.y.get())
        except Exception:
            return None
        if math.isnan(x) or math.isnan(y):
            return None
        return (x * p.um_per_unit, y * p.um_per_unit)

    # ------------------------------------------------------------ design --
    def _open_gds_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open design", self._start_dir(), "Layout (*.gds *.gds2 *.gdsii *.oas);;All files (*)")
        if path:
            self.open_gds(path)

    def _open_image_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open image", self._start_dir(),
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp);;All files (*)")
        if not path:
            return
        w, ok = QtWidgets.QInputDialog.getDouble(
            self, "Image size", "Real width of the whole image (um):", 1000.0, 0.1, 1e6, 1)
        if ok:
            self.open_image(path, w)

    def _start_dir(self):
        return str(Path(self.design.path).parent) if self.design else ""

    def open_gds(self, path, cell=None, hidden=()) -> bool:
        try:
            d = load_gds(path, cell)
        except Exception as exc:
            self.on_log(f"navigator: cannot read {path}: {exc}")
            return False
        for k in hidden:
            if k in d.layers:
                d.layers[k].visible = False
        self._set_design(d)
        n = sum(len(L.polygons) for L in d.layers.values())
        self.on_log(f"navigator: {Path(path).name}, cell {d.cell}, {n} polygons")
        return True

    def open_image(self, path, width_um) -> bool:
        reader = QtGui.QImageReader(str(path))
        reader.setAutoTransform(True)
        img = reader.read()
        if img.isNull():
            self.on_log(f"navigator: cannot read {path}: {reader.errorString()}")
            return False
        self._pixmap = QtGui.QPixmap.fromImage(img)
        self._set_design(image_design(path, img.width(), img.height(), width_um))
        self.on_log(f"navigator: {Path(path).name}, {img.width()} x {img.height()} px, "
                    f"{self.design.um_per_px:.3g} um/px")
        return True

    def _set_design(self, d: Design) -> None:
        for it in list(self._design_group.childItems()):
            self._design_group.removeFromGroup(it)
            self.scene.removeItem(it)
        self._layer_items.clear()
        self.design = d
        if d.kind == "image":
            it = QtWidgets.QGraphicsPixmapItem(self._pixmap)
            it.setTransformationMode(QtCore.Qt.SmoothTransformation)
            s = d.um_per_px
            w, h = d.image_px
            # pixel (x right, y down) -> design um (centred, y up)
            it.setTransform(QtGui.QTransform(s, 0, 0, -s, -w / 2 * s, h / 2 * s))
            self._design_group.addToGroup(it)
        else:
            for i, (key, L) in enumerate(d.layers.items()):
                path = QtGui.QPainterPath()
                path.setFillRule(QtCore.Qt.WindingFill)
                for poly in L.polygons:
                    path.addPolygon(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in poly]))
                    path.closeSubpath()
                col = QtGui.QColor(LAYER_COLOURS[i % len(LAYER_COLOURS)])
                pen = QtGui.QPen(col, 1); pen.setCosmetic(True)
                fill = QtGui.QColor(col); fill.setAlpha(90)
                it = QtWidgets.QGraphicsPathItem(path)
                it.setPen(pen); it.setBrush(fill); it.setZValue(i)
                it.setVisible(L.visible)
                self._design_group.addToGroup(it)
                self._layer_items[key] = it
        self.pick = None
        self.reg.clear()
        self._fill_layers()
        self._refresh_all()
        self.fit_design()

    def _fill_layers(self):
        d = self.design
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        if d is not None and d.kind == "gds":
            for i, (key, L) in enumerate(d.layers.items()):
                it = QtWidgets.QListWidgetItem(f"layer {key}   ({len(L.polygons)} shapes)")
                pm = QtGui.QPixmap(12, 12); pm.fill(QtGui.QColor(LAYER_COLOURS[i % len(LAYER_COLOURS)]))
                it.setIcon(QtGui.QIcon(pm))
                it.setData(QtCore.Qt.UserRole, key)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Checked if L.visible else QtCore.Qt.Unchecked)
                self.layer_list.addItem(it)
        self.layer_list.blockSignals(False)
        self.layer_list.setVisible(d is not None and d.kind == "gds")
        is_img = d is not None and d.kind == "image"
        self.width_lbl.setVisible(is_img); self.width_spin.setVisible(is_img)
        if is_img:
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(d.width_um)
            self.width_spin.blockSignals(False)

    def _layer_toggled(self, item):
        key = item.data(QtCore.Qt.UserRole)
        on = item.checkState() == QtCore.Qt.Checked
        self.design.layers[key].visible = on
        self._layer_items[key].setVisible(on)

    def _width_changed(self, w):
        d = self.design
        if d is None or d.kind != "image" or abs(w - d.width_um) < 1e-9:
            return
        had = len(self.reg.points)
        self._set_design(image_design(d.path, *d.image_px, w))
        if had:
            self.on_log("navigator: image width changed -> reference points cleared")

    def fit_design(self):
        if self.design is None:
            r = self.scene.itemsBoundingRect() if self.pair else QtCore.QRectF(-100, -100, 200, 200)
            self.canvas.fit(self._limits_item.rect() if self.pair else r)
            return
        self.canvas.fit(self._design_group.mapRectToScene(self._design_group.childrenBoundingRect()))

    def _centre_on_stage(self):
        pos = self.stage_um()
        if pos is not None:
            self.canvas.centerOn(*pos)

    # ------------------------------------------------------- registration --
    def _prior_changed(self, *_):
        self.reg.rotation_deg = self.rot_spin.value()
        self.reg.mirror = self.mirror_box.isChecked()
        self._refresh_all()

    def _model_changed(self, text):
        self.reg.model = text
        self._refresh_all()

    def add_reference(self) -> bool:
        pos = self._ready_for_reference()
        if pos is None:
            return False
        self.reg.add_point(self.pick, pos)
        n = len(self.reg.points)
        self.on_log(f"navigator: reference point {n} at design "
                    f"({_fmt_um(self.pick[0])}, {_fmt_um(self.pick[1])}) um")
        self._refresh_all()
        return True

    def correct_offset(self) -> bool:
        pos = self._ready_for_reference()
        if pos is None:
            return False
        dx, dy = self.reg.correct_offset(self.pick, pos)
        self.on_log(f"navigator: offset corrected by ({dx:+.1f}, {dy:+.1f}) um")
        self._refresh_all()
        return True

    def _ready_for_reference(self):
        if self.design is None or self.pick is None:
            self.on_log("navigator: open a design and click the feature that is under the laser")
            return None
        pos = self.stage_um()
        if pos is None:
            self.on_log("navigator: no stage position to pin it to (is a stage connected?)")
        return pos

    def _remove_point(self):
        rows = sorted({i.row() for i in self.points_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.reg.remove_point(r)
        self._refresh_all()

    def _clear_points(self):
        self.reg.clear()
        self._refresh_all()

    # -------------------------------------------------------------- click --
    def _on_click(self, x, y):
        if self.design is None:
            return
        try:
            self.pick = self.reg.to_design(x, y)
        except Exception as exc:
            self.on_log(f"navigator: {exc}")
            return
        self._refresh_pick()

    def _on_hover(self, x, y):
        txt = f"stage  {_fmt_um(x)}, {_fmt_um(y)} um"
        if self.design is not None:
            try:
                dx, dy = self.reg.to_design(x, y)
                txt += f"      design  {_fmt_um(dx)}, {_fmt_um(dy)} um"
            except Exception:
                pass
        self.hover_lbl.setText(txt)

    def target_um(self):
        """The selected point in stage um (None without one)."""
        if self.pick is None:
            return None
        try:
            return self.reg.to_stage(*self.pick)
        except Exception:
            return None

    # --------------------------------------------------------------- move --
    def go(self) -> bool:
        p, tgt = self.pair, self.target_um()
        if p is None or tgt is None:
            self.on_log("navigator: select a point, and connect a stage")
            return False
        if self._moving:
            return False
        if self.is_busy():
            self.on_log("navigator: a scan is running -- it owns the stage")
            return False
        if not p.inside(*tgt):
            self.on_log(f"navigator: ({_fmt_um(tgt[0])}, {_fmt_um(tgt[1])}) um is outside "
                        f"the {p.label} travel -- not moving")
            return False
        here = self.stage_um() or tgt
        legs = waypoints(tgt, here, self.approach.value())
        # The approach detour may start outside the travel even if the target
        # is inside; then go straight rather than clamp to somewhere else.
        if not all(p.inside(*w) for w in legs):
            legs = [tgt]
        self._moving = True
        self._stop.clear()
        self._refresh_buttons()
        self.on_log(f"navigator: {p.label} -> ({p.to_unit(tgt[0]):.6g}, "
                    f"{p.to_unit(tgt[1]):.6g}) {p.x.unit}")
        threading.Thread(target=self._move_worker, args=(p, legs),
                         name="navigator-move", daemon=True).start()
        return True

    def _move_worker(self, p, legs):
        # Settable.set BLOCKS until the module says the axis has arrived (its
        # own settle rule), so X then Y is a real sequence, not two commands
        # racing each other.
        msg = "arrived"
        try:
            for wx, wy in legs:
                for axis, v in ((p.x, wx), (p.y, wy)):
                    if self._stop.is_set():
                        raise RuntimeError("stopped")
                    axis.set(p.to_unit(v))
        except Exception as exc:
            msg = f"move ended: {exc}"
        self._bridge.done.emit(msg)

    def _move_finished(self, msg):
        self._moving = False
        self._refresh_buttons()
        self.on_log(f"navigator: {msg}")

    def stop(self):
        self._stop.set()
        p = self.pair
        if p is None or self.lab is None:
            return
        for inst in list(self.lab.instruments.values()):
            if module_prefix(inst) + "." == p.prefix:
                # off the GUI thread: a command can wait for its timeout
                threading.Thread(target=self._send_stop, args=(inst,), daemon=True).start()

    def _send_stop(self, inst):
        try:
            inst.command("stop")
        except Exception:
            pass

    # ------------------------------------------------------------ refresh --
    def _poll(self):
        if not self.isVisible():
            return
        self._pos_um = self.stage_um()
        self._update_stage_marks()
        p = self.pair
        if p is None:
            self.pos_lbl.setText("no stage connected")
        elif self._pos_um is None:
            self.pos_lbl.setText(f"{p.label}: position not available")
        else:
            x, y = self._pos_um
            self.pos_lbl.setText(f"X {p.to_unit(x):.6g}   Y {p.to_unit(y):.6g} {p.x.unit}")

    def _update_stage_marks(self):
        pos = self._pos_um
        if pos is None:
            self._stage_mark.hide(); self._fov_item.hide()
            return
        self._stage_mark.setPos(*pos); self._stage_mark.show()
        w, h = self.fov_w.value(), self.fov_h.value()
        if w > 0 and h > 0:
            self._fov_item.setRect(QtCore.QRectF(pos[0] - w / 2, pos[1] - h / 2, w, h))
            self._fov_item.show()
        else:
            self._fov_item.hide()

    def _refresh_all(self):
        # rotation / mirror boxes show the FITTED values once points decide them
        n = len(self.reg.points)
        try:
            M, t = self.reg.transform()
            ok = True
        except Exception as exc:
            ok = False
            self.fit_lbl.setText(f"cannot fit: {exc}")
        if ok:
            self._design_group.setTransform(_qtransform(M, t))
            s = self.reg.summary()
            if n >= 2:
                # The fit decides the rotation now. Keep it as the prior too, so
                # removing points later does not jump back to the old guess.
                self.reg.rotation_deg = s["rotation_deg"]
                self.rot_spin.blockSignals(True)
                self.rot_spin.setValue(s["rotation_deg"])
                self.rot_spin.blockSignals(False)
            self.mirror_box.setChecked(self.reg.mirror)
            self.fit_lbl.setText(self._fit_text(s))
        self.rot_spin.setEnabled(n < 2)
        self.mirror_box.setEnabled(n < 3)

        for m in self._ref_marks:
            self.scene.removeItem(m)
        self._ref_marks = []
        res = self.reg.residuals() if ok else []
        self.points_table.setRowCount(n)
        for i, rp in enumerate(self.reg.points):
            cells = [str(i + 1), f"{_fmt_um(rp.design[0])}, {_fmt_um(rp.design[1])}",
                     f"{res[i]:.2f}" if i < len(res) else "-"]
            for c, txt in enumerate(cells):
                self.points_table.setItem(i, c, QtWidgets.QTableWidgetItem(txt))
            if ok:
                mk = _Marker("ref", str(i + 1))
                mk.setPos(*self.reg.to_stage(*rp.design))
                self.scene.addItem(mk)
                self._ref_marks.append(mk)

        p = self.pair
        if p is not None:
            x0, y0, x1, y1 = p.limits_um()
            finite = all(math.isfinite(v) for v in (x0, y0, x1, y1))
            self._limits_item.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0) if finite
                                      else QtCore.QRectF())
        else:
            self._limits_item.setRect(QtCore.QRectF())
        d = self.design
        if d is None:
            self.design_lbl.setText("No design loaded.")
        else:
            x0, y0, x1, y1 = d.bbox()
            what = f"cell {d.cell}" if d.kind == "gds" else f"{d.image_px[0]} x {d.image_px[1]} px"
            self.design_lbl.setText(f"{Path(d.path).name}  -  {what}  -  "
                                    f"{_fmt_um(x1 - x0)} x {_fmt_um(y1 - y0)} um")
        self._refresh_pick()

    def _fit_text(self, s) -> str:
        n = s["points"]
        if n == 0:
            return ("No reference points: the picture uses your rotation and sits at the "
                    "stage origin. Put a feature under the laser, click it, 'I am here'.")
        if n == 1:
            return ("1 point: offset only, rotation as set by eye. A second point far "
                    "from the first fits the rotation.")
        scale = (f"scale {s['scale_x']:.4f}" if s["model"] == "similarity"
                 else f"scale X {s['scale_x']:.4f} / Y {s['scale_y']:.4f}, skew {s['skew_deg']:.2f} deg")
        txt = f"{s['model']} from {n} points: rotation {s['rotation_deg']:.2f} deg, {scale}"
        if s["mirror"]:
            txt += ", mirrored"
        if n >= 3:
            txt += f". Error rms {s['rms_um']:.1f} um, worst {s['max_um']:.1f} um"
        if any(s["shift_um"]):
            txt += f". Drift correction ({s['shift_um'][0]:+.1f}, {s['shift_um'][1]:+.1f}) um"
        return txt + "."

    def _refresh_pick(self):
        tgt = self.target_um()
        if tgt is None:
            self._target_mark.hide()
            self.pick_lbl.setText("Click on the design.")
        else:
            self._target_mark.setPos(*tgt); self._target_mark.show()
            lines = [f"design   {_fmt_um(self.pick[0])}, {_fmt_um(self.pick[1])} um"]
            p = self.pair
            if p is not None:
                lines.append(f"stage    {p.to_unit(tgt[0]):.6g}, {p.to_unit(tgt[1]):.6g} {p.x.unit}")
                here = self._pos_um or self.stage_um()
                if here is not None:
                    dist = math.hypot(tgt[0] - here[0], tgt[1] - here[1])
                    lines.append(f"distance {_fmt_um(dist)} um")
                if not p.inside(*tgt):
                    lines.append("<span style='color:%s'>outside the stage travel</span>" % C["danger"])
            self.pick_lbl.setText("<br>".join(lines))
        self._refresh_buttons()

    def _refresh_buttons(self):
        has_pick = self.pick is not None and self.design is not None
        has_stage = self.pair is not None
        self.go_btn.setEnabled(has_pick and has_stage and not self._moving)
        self.go_btn.setText("Moving..." if self._moving else "Go there")
        self.stop_btn.setEnabled(has_stage)
        self.here_btn.setEnabled(has_pick and has_stage and not self._moving)
        self.offset_btn.setEnabled(has_pick and has_stage and not self._moving
                                   and len(self.reg.points) > 0)

    # ------------------------------------------------------------ session --
    def _save_dialog(self):
        default = ""
        if self.design is not None:
            default = str(Path(self.design.path).with_suffix(".nav.json"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save navigator session", default, "Navigator session (*.nav.json)")
        if path:
            self.save(path)

    def save(self, path) -> None:
        extra = {"stage": self.pair.label if self.pair else "",
                 "fov_um": [self.fov_w.value(), self.fov_h.value()],
                 "approach_um": self.approach.value()}
        save_session(path, self.design, self.reg, extra)
        self.on_log(f"navigator: session saved to {path}")

    def _load_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load navigator session", self._start_dir(), "Navigator session (*.nav.json *.json)")
        if path:
            self.load(path)

    def load(self, path) -> bool:
        try:
            data = load_session(path)
        except Exception as exc:
            self.on_log(f"navigator: cannot read {path}: {exc}")
            return False
        d = data.get("design")
        if d:
            src = Path(d["path"])
            if not src.exists():
                # the session travelled with its design (another PC, a copied folder)
                alt = Path(path).parent / src.name
                src = alt if alt.exists() else src
            ok = (self.open_gds(src, d.get("cell") or None, d.get("hidden_layers", []))
                  if d["kind"] == "gds" else self.open_image(src, d["width_um"]))
            if not ok:
                return False
        self.reg = data["registration"]
        self.rot_spin.blockSignals(True); self.rot_spin.setValue(self.reg.rotation_deg)
        self.rot_spin.blockSignals(False)
        self.model_combo.blockSignals(True); self.model_combo.setCurrentText(self.reg.model)
        self.model_combo.blockSignals(False)
        fov = data.get("fov_um") or [0, 0]
        self.fov_w.setValue(fov[0]); self.fov_h.setValue(fov[1])
        self.approach.setValue(float(data.get("approach_um", 0.0)))
        i = self.stage_combo.findText(data.get("stage", ""))
        if i >= 0:
            self.stage_combo.setCurrentIndex(i)
        self._refresh_all()
        self.on_log(f"navigator: session loaded ({len(self.reg.points)} reference points). "
                    "If the sample was remounted, clear and set the points again.")
        return True
