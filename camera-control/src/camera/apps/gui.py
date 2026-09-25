"""The camera window (blueprint §7).

``run_app(ctrl, cfg, remote)`` builds a PySide6 window that drives EITHER a local
:class:`camera.camera.Camera` brain or a :class:`camera.net.client.CameraClient`
-- both expose the same methods, so the GUI never knows which it has.

Layout mirrors the LabVIEW front panel's tabs:
  * Camera        -- the live view + the primary live controls + bottom sub-tabs
                     (Control XY stage / Define scanning).
  * Spot          -- threshold the laser spot on a grabbed frame and CALIBRATE
                     its position (the one click-to-go and the stabiliser use).
  * AutoFocus     -- focus settings.
  * Pattern       -- template matching settings.
  * Camera set.   -- acquisition + image geometry + objective (pixel size).
  * Positioner    -- limits (safety envelope) + hardware wiring.
  * Readouts      -- the live numeric indicators (LabVIEW 'Hidden indicators').

A ~60 ms QTimer polls ``ctrl.status()``; brain events are forwarded to the log
through a Qt signal (they cross a thread boundary, so they MUST go via a signal).
"""

from __future__ import annotations

import math
from dataclasses import fields

from PySide6.QtCore import QLocale, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from . import theme as T
from .camera_view import CameraView
from .plots import MiniPlot
from .spot_tab import SpotTab
from ..config import AF_ROUTINES, AF_SIDES, FOCUS_MECHANISMS, SYMMETRIES, THEMES, XY_UNITS

# What a QSpinBox (a C++ int) can hold.
_INT32_MIN, _INT32_MAX = -2**31, 2**31 - 1

_ENUMS = {
    "mechanism": FOCUS_MECHANISMS,
    "routine": AF_ROUTINES,
    "approach_from": AF_SIDES,
    "symmetry": SYMMETRIES,
    "overlay_style": ("fill", "open"),
    "theme": THEMES,
    "xy_unit": XY_UNITS,
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    event = Signal(str, str)


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame(); f.setObjectName("card")
    lay = QVBoxLayout(f); lay.setContentsMargins(10, 8, 10, 10); lay.setSpacing(6)
    lab = QLabel(title.upper()); lab.setObjectName("cardTitle")
    lay.addWidget(lab)
    return f, lay


def _led(on_color: str) -> QLabel:
    lab = QLabel("  "); lab.setFixedSize(16, 16)
    lab.setStyleSheet(f"background:{T.PANEL_HI};border-radius:8px;")
    lab._on = on_color
    return lab


def _set_led(lab: QLabel, on: bool, color: str | None = None) -> None:
    c = (color or lab._on) if on else T.PANEL_HI
    lab.setStyleSheet(f"background:{c};border-radius:8px;")


def _widget_for_field(name: str, value):
    """Build an editing widget + a getter for one dataclass field."""
    if isinstance(value, bool):
        w = QCheckBox(); w.setChecked(value)
        return w, (lambda: w.isChecked())
    if name in _ENUMS:
        w = QComboBox(); w.addItems(list(_ENUMS[name]))
        i = w.findText(str(value))
        if i >= 0:
            w.setCurrentIndex(i)
        return w, (lambda: w.currentText())
    if isinstance(value, int):
        w = QSpinBox(); w.setRange(-1_000_000, 1_000_000); w.setValue(value)
        return w, (lambda: w.value())
    if isinstance(value, float):
        w = QDoubleSpinBox(); w.setRange(-1e9, 1e9); w.setDecimals(4); w.setValue(value)
        return w, (lambda: w.value())
    w = QLineEdit(str(value))
    return w, (lambda: w.text())


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
def _scrolled(widget: QWidget) -> QScrollArea:
    """Wrap a tab page so it scrolls instead of forcing the window taller."""
    area = QScrollArea()
    area.setWidgetResizable(True)       # the page still stretches to fill when it fits
    area.setFrameShape(QFrame.NoFrame)
    area.setWidget(widget)
    return area


class MainWindow(QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self._getters: dict[str, dict] = {}   # group -> {field: getter}
        self._form_widgets: dict[str, dict] = {}   # group -> {field: widget}
        self._forms: dict[str, QFormLayout] = {}   # group -> its form
        self.setWindowTitle("Camera - spot tracking, stabilisation & autofocus"
                            + (" [remote]" if remote else ""))
        self.resize(1280, 820)

        self.bridge = Bridge()
        self.bridge.event.connect(self._log_event)
        try:
            ctrl._on_event = lambda level, msg: self.bridge.event.emit(level, msg)
        except Exception:
            pass

        self.view = CameraView()
        self.view.clicked.connect(self._on_view_click)
        self.view.roi_selected.connect(self._on_roi)
        self.view.scan_area_selected.connect(self._on_scan_area)
        self._af_was_running = False

        # objective list for the dropdown (from objectives.ini via the service)
        try:
            self._objective_names = list(self.ctrl.list_objectives())
        except Exception:
            self._objective_names = []
        if not self._objective_names:
            self._objective_names = [self.cfg.image.objective_name]

        self._build_ui()

        self._timer = QTimer(self); self._timer.timeout.connect(self._refresh)
        self._timer.start(60)

    # -- UI construction --------------------------------------------------- #
    def _build_ui(self):
        tabs = QTabWidget()
        self.tabs = tabs
        # Every tab sits in its own scroll area. A QTabWidget is as tall as its
        # TALLEST tab, and the Camera tab alone needs ~1160 px -- on the lab
        # screen (1081 px usable) Qt could not honour that minimum and printed
        # "Unable to set geometry" warnings at every resize (2026-09-14). In a
        # scroll area a tab that does not fit gets a scrollbar instead.
        tabs.addTab(_scrolled(self._camera_tab()), "Camera")
        self.spot_tab = SpotTab(self.ctrl, self.cfg, self._log_event, self._frame)
        self.spot_page = _scrolled(self.spot_tab)
        tabs.addTab(self.spot_page, "Spot")
        tabs.addTab(_scrolled(self._autofocus_tab()), "AutoFocus")
        tabs.addTab(_scrolled(self._settings_tab([("Pattern", self.cfg.pattern)])), "Pattern")
        tabs.addTab(self._camera_settings_tab(), "Camera settings")   # scrolls already
        tabs.addTab(_scrolled(self._settings_tab([("Limits", self.cfg.limits),
                                                  ("Hardware", self.cfg.hardware)])), "Positioner")
        tabs.addTab(_scrolled(self._readouts_tab()), "Readouts")
        tabs.addTab(_scrolled(self._appearance_tab()), "Appearance")

        central = QWidget(); root = QVBoxLayout(central)
        root.addWidget(tabs, 1)
        self.log = QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumHeight(120)
        root.addWidget(self.log)
        self.setCentralWidget(central)
        self._label_z_fields(self._z_unit)

    def _camera_tab(self) -> QWidget:
        w = QWidget(); lay = QHBoxLayout(w)
        lay.addWidget(self.view, 3)
        right = QVBoxLayout(); lay.addLayout(right, 2)

        # Focus / Z card
        f, l = _card("Focus (Z)")
        # The spin box is the TARGET only; the live Z has its own label. It used
        # to be both: the refresh timer rewrote the box from status whenever it
        # lacked focus -- and pressing Set moves focus to the button, so the box
        # was reset to the current Z before the click landed, and Set sent the
        # position Z already had. Nothing moved (found on the KIM rig 2026-09-13).
        self.z_spin = QDoubleSpinBox(); self.z_spin.setRange(0, 75); self.z_spin.setDecimals(2)
        self._z_unit = "V"   # re-labelled from status once the Z backend reports
        self._z_target_synced = False   # fill the target from the live Z ONCE
        self.z_spin.setSuffix(" V")
        row = QHBoxLayout(); row.addWidget(QLabel("Z target"))
        row.addWidget(self.z_spin)
        b_setz = QPushButton("Set"); b_setz.clicked.connect(lambda: self.ctrl.set_z(self.z_spin.value()))
        row.addWidget(b_setz)
        self.lab_z = QLabel("at 0.00 V"); row.addWidget(self.lab_z)
        l.addLayout(row)
        # Focus steps: up = +Z by the step, down = -Z. The brain steps from the
        # last commanded target, so quick repeated clicks add up even while an
        # open-loop Z is still walking to the previous one.
        row = QHBoxLayout(); row.addWidget(QLabel("step"))
        self.z_step = QDoubleSpinBox(); self.z_step.setRange(0.001, 1000); self.z_step.setDecimals(3)
        self.z_step.setValue(1.0); self.z_step.setSuffix(" V")
        row.addWidget(self.z_step)
        self.b_z_up = QPushButton("▲  Z +"); self.b_z_up.setToolTip("focus up: Z + step")
        self.b_z_up.clicked.connect(lambda: self._step_focus(+1))
        self.b_z_dn = QPushButton("▼  Z −"); self.b_z_dn.setToolTip("focus down: Z − step")
        self.b_z_dn.clicked.connect(lambda: self._step_focus(-1))
        row.addWidget(self.b_z_up); row.addWidget(self.b_z_dn)
        l.addLayout(row)
        row2 = QHBoxLayout()
        self.b_af = QPushButton("Find focus"); self.b_af.setObjectName("primary")
        self.b_af.clicked.connect(lambda: self.ctrl.autofocus())
        b_kill = QPushButton("Kill AF"); b_kill.setObjectName("danger")
        b_kill.clicked.connect(lambda: self.ctrl.kill_af())
        row2.addWidget(self.b_af); row2.addWidget(b_kill); l.addLayout(row2)
        r3 = QHBoxLayout(); r3.addWidget(QLabel("AF")); self.led_af = _led(T.OK)
        r3.addWidget(self.led_af); self.lab_af = QLabel("OK"); r3.addWidget(self.lab_af)
        r3.addStretch(1); r3.addWidget(QLabel("Best")); self.lab_best = QLabel("0.00 V")
        r3.addWidget(self.lab_best); l.addLayout(r3)
        self.chk_cont = QCheckBox("Continuous focus")
        self.chk_cont.toggled.connect(lambda v: self.ctrl.set_continuous_focus(v))
        l.addWidget(self.chk_cont)
        right.addWidget(f)

        # Pattern / tracking card
        f, l = _card("Pattern tracking")
        r = QHBoxLayout()
        self.chk_track = QCheckBox("Allow tracking")
        self.chk_track.toggled.connect(lambda v: self.ctrl.set_tracking(v))
        r.addWidget(self.chk_track)
        r.addStretch(1); r.addWidget(QLabel("Match")); self.led_match = _led(T.OK)
        r.addWidget(self.led_match); l.addLayout(r)
        r = QHBoxLayout()
        self.chk_roi = QCheckBox("Draw template ROI")
        # Backup patterns: draw one while the main template (or a backup) is
        # matched; the offset between them is measured from that view.
        self.chk_backup = QCheckBox("Draw backup ROI")
        self.chk_roi.toggled.connect(lambda on: self._roi_mode(self.chk_roi, on))
        self.chk_backup.toggled.connect(lambda on: self._roi_mode(self.chk_backup, on))
        r.addWidget(self.chk_roi); r.addWidget(self.chk_backup); l.addLayout(r)
        r = QHBoxLayout()
        self.lab_patterns = QLabel("no backups"); self.lab_patterns.setObjectName("muted")
        b_clr = QPushButton("Clear backups"); b_clr.clicked.connect(self._clear_backups)
        r.addWidget(self.lab_patterns, 1); r.addWidget(b_clr); l.addLayout(r)
        r = QHBoxLayout()
        b_load = QPushButton("Load pattern"); b_load.clicked.connect(self._load_pattern)
        b_save = QPushButton("Save pattern"); b_save.clicked.connect(self._save_pattern)
        r.addWidget(b_load); r.addWidget(b_save); l.addLayout(r)
        right.addWidget(f)

        # Stabiliser card
        f, l = _card("Stabiliser")
        r = QHBoxLayout()
        self.chk_stab = QCheckBox("Stabilise")
        self.chk_stab.toggled.connect(lambda v: self.ctrl.set_stabilize(v))
        r.addWidget(self.chk_stab)
        r.addStretch(1); r.addWidget(QLabel("Stable")); self.led_stable = _led(T.OK)
        r.addWidget(self.led_stable); l.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Index X")); self.sp_ix = QSpinBox(); self.sp_ix.setRange(0, 999)
        r.addWidget(self.sp_ix)
        r.addWidget(QLabel("Y")); self.sp_iy = QSpinBox(); self.sp_iy.setRange(0, 999)
        r.addWidget(self.sp_iy)
        b_idx = QPushButton("Select"); b_idx.clicked.connect(
            lambda: self.ctrl.set_selected_index(self.sp_ix.value(), self.sp_iy.value()))
        r.addWidget(b_idx); l.addLayout(r)
        # How it behaves (Lukáš: "faster, more bold, and a distance that counts
        # as stable"). Edits go to the brain 300 ms after the last change, no
        # Apply button: you tune it while watching it work.
        g = QGridLayout(); g.setHorizontalSpacing(6)
        stb = self.cfg.stabilizer
        self.stab_gain = QDoubleSpinBox(); self.stab_gain.setRange(5, 150)
        self.stab_gain.setDecimals(0); self.stab_gain.setSingleStep(10); self.stab_gain.setSuffix(" %")
        self.stab_gain.setValue(stb.gain * 100)
        self.stab_gain.setToolTip("fraction of the measured error corrected per move: "
                                  "100 % = all at once, > 100 % overshoots")
        self.stab_frames = QSpinBox(); self.stab_frames.setRange(1, 50)
        self.stab_frames.setValue(stb.images_to_average)
        self.stab_frames.setToolTip("frames averaged per measurement: fewer = faster, noisier")
        self.stab_radius = QDoubleSpinBox(); self.stab_radius.setRange(0.0, 100.0)
        self.stab_radius.setDecimals(3); self.stab_radius.setSingleStep(0.05)
        self.stab_radius.setSuffix(" um"); self.stab_radius.setValue(stb.stable_radius_um)
        self.stab_radius.setToolTip("closer than this to the point = Stable, and no correction")
        self.stab_settle = QDoubleSpinBox(); self.stab_settle.setRange(0.0, 5.0)
        self.stab_settle.setDecimals(2); self.stab_settle.setSingleStep(0.05)
        self.stab_settle.setSuffix(" s"); self.stab_settle.setValue(stb.settle_s)
        self.stab_settle.setToolTip("wait after each move before measuring again")
        g.addWidget(QLabel("correct"), 0, 0); g.addWidget(self.stab_gain, 0, 1)
        g.addWidget(QLabel("average"), 0, 2); g.addWidget(self.stab_frames, 0, 3)
        g.addWidget(QLabel("stable within"), 1, 0); g.addWidget(self.stab_radius, 1, 1)
        g.addWidget(QLabel("settle"), 1, 2); g.addWidget(self.stab_settle, 1, 3)
        l.addLayout(g)
        self.lab_stab = QLabel("distance -"); self.lab_stab.setObjectName("muted")
        l.addWidget(self.lab_stab)
        self._stab_timer = QTimer(self); self._stab_timer.setSingleShot(True)
        self._stab_timer.setInterval(300)
        self._stab_timer.timeout.connect(self._apply_stabiliser)
        for w_ in (self.stab_gain, self.stab_frames, self.stab_radius, self.stab_settle):
            w_.valueChanged.connect(lambda _v: self._stab_timer.start())
        right.addWidget(f)

        # Imaging card
        f, l = _card("Imaging")
        r = QHBoxLayout()
        b_snap = QPushButton("Snapshot"); b_snap.clicked.connect(lambda: self.ctrl.snapshot())
        self.chk_click = QCheckBox("Click to go")
        r.addWidget(b_snap); r.addWidget(self.chk_click); l.addLayout(r)
        self.chk_thr = QCheckBox("Show threshold / spot area")
        self.chk_thr.toggled.connect(self.view.set_show_threshold)
        l.addWidget(self.chk_thr)
        r = QHBoxLayout()
        self.chk_spot_info = QCheckBox("Label spot area")
        self.chk_spot_info.toggled.connect(self.view.set_show_spot_info)
        self.chk_pat_info = QCheckBox("Label pattern (score, x/y, distance)")
        self.chk_pat_info.toggled.connect(self.view.set_show_pattern_info)
        r.addWidget(self.chk_spot_info); r.addWidget(self.chk_pat_info); r.addStretch(1)
        l.addLayout(r)
        self.chk_points = QCheckBox("Show scan points")
        self.chk_points.setChecked(True)
        self.chk_points.setToolTip("While stabilising, a pink x marks the point it aims at, "
                                   "shown or not")
        self.chk_points.toggled.connect(self.view.set_show_scan_points)
        l.addWidget(self.chk_points)
        right.addWidget(f)
        right.addStretch(1)

        # bottom sub-tabs
        sub = QTabWidget()
        sub.addTab(self._xy_subtab(), "Control XY stage")
        sub.addTab(self._scanning_subtab(), "Define scanning")
        sub.addTab(self._accuracy_subtab(), "Check alignment accuracy")
        outer = QWidget(); ov = QVBoxLayout(outer)
        ov.addWidget(w, 3); ov.addWidget(sub, 1)
        return outer

    def _scanning_subtab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.chk_scan = QCheckBox("Draw / edit scan area")
        self.chk_scan.toggled.connect(self.view.set_scan_mode)
        b_recall = QPushButton("Recall ROI")
        b_recall.clicked.connect(self._recall_scan_rect)
        b_clear = QPushButton("Clear ROI")
        b_clear.clicked.connect(self.view.clear_scan_rect)
        row.addWidget(self.chk_scan); row.addWidget(b_recall)
        row.addWidget(b_clear); row.addStretch(1)
        v.addLayout(row)
        v.addWidget(QLabel("Drag an empty area to draw a new rectangle; drag the "
                           "body to move, a corner to resize, the top knob to tilt. "
                           "'Recall ROI' rebuilds the rectangle from the stored array."))
        # Alternative: set the array by total SIZE (um); pitch adapts to points.
        f, l = _card("Array size (um) -> pitch")
        r = QHBoxLayout()
        self.sp_sizex = QDoubleSpinBox(); self.sp_sizex.setRange(0, 100000); self.sp_sizex.setDecimals(2)
        self.sp_sizey = QDoubleSpinBox(); self.sp_sizey.setRange(0, 100000); self.sp_sizey.setDecimals(2)
        r.addWidget(QLabel("X size")); r.addWidget(self.sp_sizex)
        r.addWidget(QLabel("Y size")); r.addWidget(self.sp_sizey)
        b_size = QPushButton("Apply size"); b_size.clicked.connect(self._apply_scan_size)
        r.addWidget(b_size); l.addLayout(r)
        v.addWidget(f)
        v.addWidget(self._settings_tab([("Scanning", self.cfg.scanning)], compact=True))
        return w

    def _sync_size_fields(self):
        """Refresh the array-size (um) fields from config -- called only when the
        scan area actually changes (draw/edit/recall/apply), NEVER on the poll
        timer, so it can't clobber a value the user is about to Apply."""
        if not hasattr(self, "sp_sizex"):
            return
        if self.sp_sizex.hasFocus() or self.sp_sizey.hasFocus():
            return
        sc = self.cfg.scanning
        for sp, span in ((self.sp_sizex, (sc.points_x - 1) * sc.dx_um),
                         (self.sp_sizey, (sc.points_y - 1) * sc.dy_um)):
            sp.blockSignals(True)
            sp.setValue(max(0.0, span))
            sp.blockSignals(False)

    def _recall_scan_rect(self):
        try:
            rect = self.ctrl.get_scan_rect()
        except Exception as exc:
            self._log_event("error", f"recall failed: {exc}")
            return
        if not rect:
            self._log_event("warn", "no ROI to recall yet (track a template first)")
            return
        self.view.set_recalled_rect(rect)
        self.chk_scan.setChecked(True)
        self.ctrl_get_config_into_cfg()
        self._sync_size_fields()
        self._sync_form("scanning")
        self._log_event("info", "recalled scan ROI - drag the handles to edit")

    def _apply_scan_size(self):
        try:
            self.ctrl.set_scan_size_um(self.sp_sizex.value(), self.sp_sizey.value())
            self.ctrl_get_config_into_cfg()
            self._sync_form("scanning")       # the size set new dx, dy: show them
            self._log_event("info", f"array size applied "
                            f"({self.sp_sizex.value():.1f} x {self.sp_sizey.value():.1f} um)")
        except Exception as exc:
            self._log_event("error", f"apply size failed: {exc}")

    def _accuracy_subtab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.chk_acc = QCheckBox("Log accuracy (residual spot->point, um)")
        self.chk_acc.toggled.connect(lambda o: self.ctrl.set_accuracy_logging(o))
        b_clr = QPushButton("Clear")
        b_clr.clicked.connect(lambda: self.ctrl.set_accuracy_logging(self.chk_acc.isChecked()))
        row.addWidget(self.chk_acc); row.addWidget(b_clr); row.addStretch(1)
        v.addLayout(row)
        self.acc_plot = MiniPlot(xlabel="sample", ylabel="um")
        v.addWidget(self.acc_plot, 1)
        self.lab_acc = QLabel("dX rms: -   dY rms: -")
        v.addWidget(self.lab_acc)
        return w

    def _autofocus_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        note = QLabel("The spot threshold (also used by the spot_area focus metric) is set "
                      "on the Spot tab.")
        note.setObjectName("muted")
        v.addWidget(note)
        v.addWidget(self._settings_tab([("Autofocus", self.cfg.autofocus)]))
        f, l = _card("Focus sweep (metric vs Z voltage)")
        self.af_plot = MiniPlot(xlabel="Z (V)", ylabel="metric")
        self.af_plot.setMinimumHeight(170)
        l.addWidget(self.af_plot)
        b = QPushButton("Show last sweep"); b.clicked.connect(self._update_af_plot)
        l.addWidget(b)
        v.addWidget(f)
        return w

    def _xy_subtab(self) -> QWidget:
        # Three columns side by side, so the whole thing fits the short strip
        # under the live view: where the stage IS | jog pad | go to + datum.
        # Three compact cards side by side -- POSITION | JOG | GO TO -- each at
        # its natural size and pinned top-left. Stretch factors spread the first
        # version over the full 2000 px width with buttons floating in space.
        w = QWidget(); lay = QHBoxLayout(w); lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)
        BTN_W, BOX_W = 84, 110

        # -- live position (read-only: never an input, see the Z target note) --
        # FIXED width: the texts change length while the stage moves (more
        # digits, "moving"), and a card sized to its text made the Jog card jump
        # sideways on every click (Lukáš, 2026-09-14).
        f, l = _card("Position")
        f.setFixedWidth(300)
        self.lab_stage_x = QLabel("X  -"); self.lab_stage_y = QLabel("Y  -")
        for lab in (self.lab_stage_x, self.lab_stage_y):
            lab.setStyleSheet("font-size:18px; font-weight:600;")
            l.addWidget(lab)
        self.lab_xy = QLabel("stage"); self.lab_xy.setObjectName("muted")
        self.lab_pxsize = QLabel("pixel: 0.000 um"); self.lab_pxsize.setObjectName("muted")
        # "moving" gets its own line that is always there (blank when still), so
        # it never changes the card's size either
        self.lab_moving = QLabel(" "); self.lab_moving.setStyleSheet(f"color:{T.ACCENT};")
        l.addWidget(self.lab_xy); l.addWidget(self.lab_pxsize); l.addWidget(self.lab_moving)
        l.addStretch(1)
        lay.addWidget(f, 0, Qt.AlignTop)

        # -- jog pad: stage axes, not image directions (the kim calibration owns
        #    the image mapping). The step unit follows the stage: steps on kim.
        f, l = _card("Jog")
        jog = QGridLayout(); jog.setSpacing(4)
        self._xy_unit = "um"
        self.xy_step = QDoubleSpinBox(); self.xy_step.setRange(0.001, 100000)
        self.xy_step.setDecimals(3); self.xy_step.setValue(1.0); self.xy_step.setSuffix(" um")
        self.xy_step.setFixedWidth(BOX_W); self.xy_step.setToolTip("jog step")
        self.b_y_up = QPushButton("▲  Y +"); self.b_y_dn = QPushButton("▼  Y −")
        self.b_x_dn = QPushButton("◀  X −"); self.b_x_up = QPushButton("X +  ▶")
        for b, sx, sy in ((self.b_x_up, 1, 0), (self.b_x_dn, -1, 0),
                          (self.b_y_up, 0, 1), (self.b_y_dn, 0, -1)):
            b.setFixedWidth(BTN_W)
            b.clicked.connect(lambda _=False, sx=sx, sy=sy: self._jog_xy(sx, sy))
        jog.addWidget(self.b_y_up, 0, 1, Qt.AlignCenter)
        jog.addWidget(self.b_x_dn, 1, 0); jog.addWidget(self.xy_step, 1, 1, Qt.AlignCenter)
        jog.addWidget(self.b_x_up, 1, 2)
        jog.addWidget(self.b_y_dn, 2, 1, Qt.AlignCenter)
        l.addLayout(jog)
        l.addStretch(1)
        lay.addWidget(f, 0, Qt.AlignTop)

        # -- absolute move + datum ------------------------------------------
        f, l = _card("Go to / datum")
        g = QGridLayout(); g.setSpacing(4)
        self.sp_x = QDoubleSpinBox(); self.sp_y = QDoubleSpinBox()
        for r_, (name, sp) in enumerate((("X", self.sp_x), ("Y", self.sp_y))):
            sp.setRange(-1e6, 1e6); sp.setDecimals(2); sp.setSuffix(" um")
            sp.setFixedWidth(BOX_W)
            g.addWidget(QLabel(name), r_, 0); g.addWidget(sp, r_, 1)
        b = QPushButton("Move"); b.setFixedWidth(BTN_W); b.clicked.connect(self._move_xy_abs)
        g.addWidget(b, 0, 2, 2, 1, Qt.AlignVCenter)
        l.addLayout(g)
        self.b_datum = QPushButton("Datum XY  (zero here)"); self.b_datum.setObjectName("danger")
        self.b_datum.setToolTip("Reset the stage's X and Y step counters to 0 at the "
                                "current position (kim Datum).")
        self.b_datum.clicked.connect(self._datum_xy)
        l.addWidget(self.b_datum)
        l.addStretch(1)
        lay.addWidget(f, 0, Qt.AlignTop)

        lay.addStretch(1)                       # spare width stays on the right
        return w

    # Autofocus fields whose value is in the Z device's unit. Their config names
    # still end in "_v" (the piezo rig drives Z in volts; renaming touches the
    # wire and every INI), so the FORM LABEL says the real unit instead.
    _Z_UNIT_FIELDS = {"drive_amplitude_v": "drive_amplitude",
                      "offset_from_found_v": "offset_from_found",
                      "approach_margin": "approach_margin",
                      "coarse_step_v": "coarse_step",
                      "fine_step_v": "fine_step",
                      "max_travel_v": "max_travel"}

    def _label_z_fields(self, unit: str):
        form = self._forms.get("autofocus")
        widgets = self._form_widgets.get("autofocus", {})
        if form is None:
            return
        for name, text in self._Z_UNIT_FIELDS.items():
            w = widgets.get(name)
            lab = form.labelForField(w) if w is not None else None
            if lab is not None:
                lab.setText(f"{text} ({unit})")

    def _apply_stabiliser(self):
        vals = {"gain": self.stab_gain.value() / 100.0,
                "images_to_average": self.stab_frames.value(),
                "stable_radius_um": self.stab_radius.value(),
                "settle_s": self.stab_settle.value()}
        try:
            self.ctrl.set_config({"stabilizer": vals})
            for k, v in vals.items():
                setattr(self.cfg.stabilizer, k, v)
            self._sync_form("stabilizer")
            self._log_event("info", "stabiliser: correct {:.0f} %, average {}, stable within "
                            "{:.3f} um, settle {:.2f} s".format(
                                vals["gain"] * 100, vals["images_to_average"],
                                vals["stable_radius_um"], vals["settle_s"]))
        except Exception as exc:
            self._log_event("error", f"stabiliser settings: {exc}")

    def _on_xy_unit_changed(self, unit: str):
        try:
            self.ctrl.set_config({"hardware": {"xy_unit": unit}})
            self.cfg.hardware.xy_unit = unit
            self._log_event("info", f"XY shown and jogged in {unit}")
        except Exception as exc:
            self._log_event("error", f"XY unit: {exc}")

    def _refresh_xy(self, s):
        self.lab_stab.setText(f"distance {s.distance_um:.3f} um   "
                              f"(stable within {self.cfg.stabilizer.stable_radius_um:.3f} um)")
        self.lab_moving.setText("● moving" if s.stage_moving else " ")
        if s.xy_step_unit == "steps":
            self.lab_stage_x.setText(f"X  {s.stage_steps_x:+d} steps")
            self.lab_stage_y.setText(f"Y  {s.stage_steps_y:+d} steps")
            # kim's um = steps x a nominal um/step, not yet measured on this rig
            self.lab_xy.setText(f"({s.stage_x:.2f}, {s.stage_y:.2f}) um nominal")
        else:
            self.lab_stage_x.setText(f"X  {s.stage_x:.3f} um")
            self.lab_stage_y.setText(f"Y  {s.stage_y:.3f} um")
            if s.xy_has_datum:      # a step-counting stage shown in um: say what um means
                self.lab_xy.setText(f"({s.stage_steps_x:+d}, {s.stage_steps_y:+d}) steps x "
                                    f"kim um/step")
            else:
                self.lab_xy.setText("stage")
        if s.xy_step_unit != self._xy_unit:
            self._xy_unit = s.xy_step_unit
            if s.xy_step_unit == "steps":
                self.xy_step.setDecimals(0); self.xy_step.setValue(100)
                self.xy_step.setSuffix(" steps")
            else:
                self.xy_step.setDecimals(3); self.xy_step.setValue(1.0)
                self.xy_step.setSuffix(" um")
        self.b_datum.setEnabled(s.xy_has_datum)
        # Positioner tab: on a stage that owns its limits, cfg.limits is not used
        lim_rows = getattr(self, "_limit_rows", {})
        for name, row_widget in lim_rows.items():
            self._forms["limits"].setRowVisible(row_widget, not s.limits_from_stage)
        note = getattr(self, "lab_limits_note", None)
        if note is not None:
            note.setVisible(s.limits_from_stage)
            if s.limits_from_stage:
                note.setText(
                    "The XY/Z stage sets its own travel limits (kim limits / leash); the "
                    "camera clamps to them and ignores its own envelope.\n"
                    f"X {s.x_min:.1f} … {s.x_max:.1f} um    "
                    f"Y {s.y_min:.1f} … {s.y_max:.1f} um    "
                    f"Z {s.z_min:.1f} … {s.z_max:.1f} {s.z_unit}\n"
                    "Change them in the kim GUI (Settings / leash).")

    def _jog_xy(self, sx: int, sy: int):
        d = self.xy_step.value()
        if self._xy_unit == "steps":
            d = round(d)
        try:
            self.ctrl.step_xy(sx * d, sy * d)
        except Exception as exc:
            self._log_event("warn", f"XY jog: {exc}")

    def _move_xy_abs(self):
        try:
            self.ctrl.move_xy(self.sp_x.value(), self.sp_y.value())
        except Exception as exc:
            self._log_event("warn", f"XY move: {exc}")

    def _datum_xy(self):
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self, "Datum XY",
            "Make the current position X = 0, Y = 0 (reset the step counters)?\n\n"
            "Stage coordinates noted before this, and kim's leash box, will refer "
            "to the new origin.")
        if ans != QMessageBox.Yes:
            return
        try:
            self.ctrl.datum_xy()
        except Exception as exc:
            self._log_event("warn", f"datum: {exc}")

    def _settings_tab(self, groups, compact=False) -> QWidget:
        w = QWidget(); outer = QVBoxLayout(w)
        for gname, obj in groups:
            f, l = _card(gname)
            form = QFormLayout()
            getters = {}
            for fld in fields(obj):
                if fld.name == "objective_name":
                    widget = QComboBox()
                    widget.addItems(self._objective_names)
                    cur = str(getattr(obj, fld.name))
                    idx = widget.findText(cur)
                    widget.setCurrentIndex(idx if idx >= 0 else 0)
                    # connect AFTER setting the index so setup doesn't fire it
                    widget.currentTextChanged.connect(self._on_objective_changed)
                    self._objective_combo = widget
                    getter = widget.currentText
                else:
                    widget, getter = _widget_for_field(fld.name, getattr(obj, fld.name))
                if fld.name == "xy_unit":
                    # a display choice: takes effect at once, no Apply needed
                    widget.currentTextChanged.connect(self._on_xy_unit_changed)
                form.addRow(fld.name, widget)
                getters[fld.name] = getter
                self._form_widgets.setdefault(gname.lower(), {})[fld.name] = widget
                if fld.name == "pixel_size_x_um":
                    self._pxx_widget = widget
                elif fld.name == "pixel_size_y_um":
                    self._pxy_widget = widget
            self._getters[gname.lower()] = getters
            self._forms[gname.lower()] = form
            if gname == "Limits":
                # Shown instead of the envelope rows when the stage owns its
                # limits (the KIM rig): 0..130 um / 0..75 V mean nothing there.
                self.lab_limits_note = QLabel(""); self.lab_limits_note.setWordWrap(True)
                self.lab_limits_note.setVisible(False)
                l.addWidget(self.lab_limits_note)
                self._limit_rows = {k: v for k, v in self._form_widgets["limits"].items()
                                    if k != "enforce"}
            l.addLayout(form)
            outer.addWidget(f)
        b = QPushButton("Apply settings"); b.setObjectName("primary")
        b.clicked.connect(lambda _=False, gs=groups: self._apply_settings(gs))
        outer.addWidget(b)
        outer.addStretch(1)
        return w

    def _camera_settings_tab(self) -> QWidget:
        outer = QWidget(); ov = QVBoxLayout(outer)
        # Order (Lukas, 2026-09-14): Image first (objective / pixel size, used all
        # the time), then Camera, then the long list of live camera parameters.
        ov.addWidget(self._settings_tab([("Image", self.cfg.image),
                                         ("Camera", self.cfg.camera)]))
        # live camera parameters (built from the backend's feature list)
        f, l = _card("Camera parameters (live)")
        row = QHBoxLayout()
        row.addWidget(QLabel("Read live from the camera; changes apply immediately."))
        row.addStretch(1)
        b = QPushButton("Refresh"); b.clicked.connect(self._build_cam_params)
        row.addWidget(b)
        l.addLayout(row)
        self._cam_param_host = QWidget()
        self._cam_param_form = QFormLayout(self._cam_param_host)
        l.addWidget(self._cam_param_host)
        ov.addWidget(f)
        ov.addStretch(1)
        self._build_cam_params()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(outer)
        return scroll

    def _build_cam_params(self):
        self._clear_layout(self._cam_param_form)
        try:
            feats = self.ctrl.camera_features()
        except Exception as exc:
            self._cam_param_form.addRow(QLabel(f"(camera features unavailable: {exc})"))
            return
        if not feats:
            self._cam_param_form.addRow(QLabel("(this camera exposes no parameters here)"))
            return
        for ft in feats:
            label = ft.get("display", ft.get("name", "?"))
            # One odd feature must not take the whole window down: a real camera
            # exposes hundreds, with ranges the simulator never had.
            try:
                w = self._feature_widget(ft)
            except Exception as exc:
                self._cam_param_form.addRow(label, QLabel(f"(not shown: {type(exc).__name__}: {exc})"))
                continue
            if w is None:
                continue
            unit = f"  ({ft['unit']})" if ft.get("unit") else ""
            self._cam_param_form.addRow(label + unit, w)

    def _feature_widget(self, ft):
        name, t = ft["name"], ft["type"]
        writable = ft.get("writable", True)
        if t == "command":
            w = QPushButton(ft.get("display", name)); w.setEnabled(writable)
            w.clicked.connect(lambda _=False, n=name: self._set_cam_feature(n, True))
            return w
        if t == "bool":
            w = QCheckBox(); w.setChecked(bool(ft.get("value"))); w.setEnabled(writable)
            if writable:
                w.toggled.connect(lambda v, n=name: self._set_cam_feature(n, v))
            return w
        if t == "enum":
            w = QComboBox(); w.addItems([str(o) for o in (ft.get("options") or [])])
            i = w.findText(str(ft.get("value"))); w.setCurrentIndex(i if i >= 0 else 0)
            w.setEnabled(writable)
            if writable:   # 'activated' fires only on user action, not setCurrentIndex
                w.activated.connect(lambda _i, c=w, n=name: self._set_cam_feature(n, c.currentText()))
            return w
        if t == "int":
            lo = int(ft["min"]) if ft.get("min") is not None else -10**9
            hi = int(ft["max"]) if ft.get("max") is not None else 10**9
            if _INT32_MIN <= lo and hi <= _INT32_MAX:
                w = QSpinBox()
            else:
                # QSpinBox is a signed 32-bit int, but GenICam integers are
                # 64-bit and IDS cameras report ranges up to 2**32 - 1 (the lab
                # U3-386xCP-M crashed the GUI with OverflowError). A double spin
                # box with no decimals is exact for every integer below 2**53.
                w = QDoubleSpinBox(); w.setDecimals(0)
            w.setRange(lo, hi)
            if ft.get("inc"):
                w.setSingleStep(max(1, int(ft["inc"])))
            w.setValue(int(ft.get("value") or 0)); w.setEnabled(writable)
            if writable:
                w.editingFinished.connect(lambda s=w, n=name: self._set_cam_feature(n, s.value()))
            return w
        if t == "float":
            w = QDoubleSpinBox()
            w.setRange(float(ft["min"]) if ft.get("min") is not None else -1e12,
                       float(ft["max"]) if ft.get("max") is not None else 1e12)
            inc = ft.get("inc")
            if inc:
                w.setDecimals(0 if inc >= 1 else min(6, max(1, math.ceil(-math.log10(inc)))))
                w.setSingleStep(float(inc))
            else:
                w.setDecimals(3)
            w.setValue(float(ft.get("value") or 0)); w.setEnabled(writable)
            if writable:
                w.editingFinished.connect(lambda s=w, n=name: self._set_cam_feature(n, s.value()))
            return w
        # string
        w = QLineEdit(str(ft.get("value", ""))); w.setReadOnly(not writable)
        if writable:
            w.editingFinished.connect(lambda e=w, n=name: self._set_cam_feature(n, e.text()))
        return w

    def _set_cam_feature(self, name, value):
        try:
            self.ctrl.set_camera_feature(name, value)   # brain emits an info event
        except Exception as exc:
            self._log_event("warn", f"set {name} failed: {exc}")

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _appearance_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(QLabel("Theme is applied on the next launch (start-up setting). "
                           "Pick a theme and click Apply settings."))
        v.addWidget(self._settings_tab([("UI", self.cfg.ui)]))
        v.addStretch(1)
        return w

    def _readouts_tab(self) -> QWidget:
        w = QWidget(); g = QGridLayout(w)
        self._ro = {}
        names = ["frame_number", "fps", "spot_x", "spot_y", "spot_area",
                 "template_x", "template_y", "match_score", "distance_um",
                 "selected_point_x", "selected_point_y", "spot_at_index_x",
                 "spot_at_index_y", "z_voltage", "best_focus_v", "stage_x",
                 "stage_y", "pixel_size_x", "objective_name"]
        for i, n in enumerate(names):
            g.addWidget(QLabel(n), i // 2, (i % 2) * 2)
            lab = QLabel("-"); lab.setObjectName("caption")
            g.addWidget(lab, i // 2, (i % 2) * 2 + 1)
            self._ro[n] = lab
        return w

    # -- actions ----------------------------------------------------------- #
    def _apply_settings(self, groups):
        payload = {}
        for gname, _obj in groups:
            key = gname.lower()
            payload[key] = {n: get() for n, get in self._getters.get(key, {}).items()}
        if "scanning" in payload:
            self._keep_scan_size(payload["scanning"])
        try:
            self.ctrl.set_config(payload)
            # keep our local cfg mirror in step (used to draw overlays)
            self.ctrl_get_config_into_cfg()
            self._sync_size_fields()          # pitch/points edits update size too
            if "scanning" in payload:
                self._sync_form("scanning")   # show the pitch a points change produced
            self._log_event("info", f"applied settings: {', '.join(payload)}")
        except Exception as exc:
            self._log_event("error", f"apply failed: {exc}")

    def _keep_scan_size(self, new: dict) -> None:
        """Changing only the NUMBER OF POINTS keeps the array size: the pitch
        follows (Lukáš, 2026-09-14). A dx/dy typed in the same Apply wins, and
        then the size follows the pitch as before."""
        sc = self.cfg.scanning
        for pts, step in (("points_x", "dx_um"), ("points_y", "dy_um")):
            n_old, n_new = int(getattr(sc, pts)), int(new.get(pts, getattr(sc, pts)))
            pitch_untouched = abs(float(new.get(step, getattr(sc, step)))
                                  - float(getattr(sc, step))) < 6e-5  # the form shows 4 dp
            if n_new != n_old and pitch_untouched and n_old > 1 and n_new > 1:
                span = (n_old - 1) * float(getattr(sc, step))
                new[step] = span / (n_new - 1)

    def _sync_form(self, group: str) -> None:
        """Write the config values of ``group`` into its settings form.

        The forms are filled once when the window is built. Anything that changes
        those values from ELSEWHERE -- drawing the scan area, "Apply size",
        Recall -- must call this, or the form keeps the old numbers and the next
        "Apply settings" sends them back (found 2026-09-14: a drawn scan area's
        rotation reset to 0, and its dx/dy reverted, on Apply settings).
        Called on those events only, never on the poll timer.
        """
        obj = getattr(self.cfg, group, None)
        for name, w in self._form_widgets.get(group, {}).items():
            if obj is None or not hasattr(obj, name) or w.hasFocus():
                continue
            val = getattr(obj, name)
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, QComboBox):
                    i = w.findText(str(val))
                    if i >= 0:
                        w.setCurrentIndex(i)
                elif isinstance(w, QSpinBox):
                    w.setValue(int(val))
                elif isinstance(w, QDoubleSpinBox):
                    w.setValue(float(val))
                elif isinstance(w, QLineEdit):
                    w.setText(str(val))
            finally:
                w.blockSignals(False)

    def ctrl_get_config_into_cfg(self):
        try:
            data = self.ctrl.get_config()
            data = data if isinstance(data, dict) else None
        except Exception:
            data = None
        if not data:
            return
        for gname, values in data.items():
            obj = getattr(self.cfg, gname, None)
            if obj is None:
                continue
            for k, v in values.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)

    def _on_objective_changed(self, name):
        """Picking an objective sets the pixel size from objectives.ini."""
        if not name:
            return
        try:
            res = self.ctrl.set_objective(name)
        except Exception as exc:
            self._log_event("error", f"set objective failed: {exc}")
            return
        self.cfg.image.objective_name = res.get("objective", name)
        self.cfg.image.pixel_size_x_um = res.get("pixel_size_x_um", self.cfg.image.pixel_size_x_um)
        self.cfg.image.pixel_size_y_um = res.get("pixel_size_y_um", self.cfg.image.pixel_size_y_um)
        # reflect the derived pixel size in its (read-back) spinboxes
        for w, val in ((getattr(self, "_pxx_widget", None), self.cfg.image.pixel_size_x_um),
                       (getattr(self, "_pxy_widget", None), self.cfg.image.pixel_size_y_um)):
            if w is not None:
                w.blockSignals(True); w.setValue(val); w.blockSignals(False)
        self._log_event("info", f"objective: {name} "
                        f"({self.cfg.image.pixel_size_x_um:.4f} um/px)")

    def _step_focus(self, sign: int):
        try:
            self.ctrl.step_z(sign * self.z_step.value())
        except Exception as exc:
            self._log_event("warn", f"focus step: {exc}")

    def _on_view_click(self, x, y):
        if self.chk_click.isChecked():
            try:
                self.ctrl.click_to_go(x, y)
            except Exception as exc:
                self._log_event("warn", f"click-to-go: {exc}")

    def _roi_mode(self, which: QCheckBox, on: bool):
        """Template and backup ROI share the view's ROI tool: one at a time."""
        other = self.chk_backup if which is self.chk_roi else self.chk_roi
        if on and other.isChecked():
            other.blockSignals(True); other.setChecked(False); other.blockSignals(False)
        self.view.set_roi_mode(self.chk_roi.isChecked() or self.chk_backup.isChecked())

    def _on_roi(self, cx, cy, w, h):
        backup = self.chk_backup.isChecked()
        try:
            if backup:
                desc = self.ctrl.capture_backup((cx, cy, w, h))
                self._log_event("info", desc)
                self.chk_backup.setChecked(False)
            else:
                desc = self.ctrl.capture_reference((cx, cy, w, h))
                self._log_event("info", f"captured template: {desc}")
                self.chk_roi.setChecked(False)
        except Exception as exc:
            self._log_event("error", f"{'backup' if backup else 'capture'} failed: {exc}")

    def _clear_backups(self):
        try:
            self.ctrl.clear_backups()
        except Exception as exc:
            self._log_event("error", f"clear backups: {exc}")

    def _on_scan_area(self, cx, cy, w, h, angle):
        try:
            res = self.ctrl.set_scan_area(cx, cy, w, h, angle)
            self._log_event("info",
                            f"scan area {res['size_x_um']:.1f}x{res['size_y_um']:.1f} um "
                            f"@ {res['angle_deg']:.1f} deg -> pitch "
                            f"({res['dx_um']:.2f}, {res['dy_um']:.2f}) um")
            self.ctrl_get_config_into_cfg()
            self._sync_size_fields()          # a draw/edit updates the size fields...
            self._sync_form("scanning")       # ...and dx, dy, angle in the Scanning form
        except Exception as exc:
            self._log_event("error", f"scan area failed: {exc}")

    def _update_af_plot(self):
        try:
            c = self.ctrl.get_af_curve()
        except Exception:
            return
        if not c.get("z"):
            return
        def finite(zz, mm):
            # levels where the spot was not seen come back as NaN: leave them out
            pts = [(z, m) for z, m in zip(zz, mm) if m is not None and math.isfinite(m)]
            return [p[0] for p in pts], [p[1] for p in pts]

        self.af_plot._xlabel = f"Z ({self._z_unit})"
        label = "spot area (px²)" if self.cfg.autofocus.mechanism == "spot_area" else "focus"
        phases = c.get("phases")
        if phases:
            # one-way routine: coarse search, fine walk, park walk -- each its own
            # colour, so you can see where it searched and where it stopped
            series = []
            for key, color, name in (("coarse", T.MUTED, "coarse"),
                                     ("fine", T.ACCENT_HI, label),
                                     ("park", T.OK, "park")):
                zs, ms = finite(phases.get(key, {}).get("z", []),
                                phases.get(key, {}).get("metric", []))
                if zs:
                    series.append((zs, ms, color, name))
            if series:
                self.af_plot.set_series(series, vline=c.get("best"))
            return
        zs, ms = finite(c["z"], c["metric"])
        if not zs:
            return
        self.af_plot.set_series([(zs, ms, T.ACCENT_HI, label)], vline=c.get("best"))

    def _load_pattern(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load pattern", "", "PNG (*.png)")
        if path:
            try:
                self.ctrl.load_pattern(path)
            except Exception as exc:
                self._log_event("error", f"load failed: {exc}")
                return
            # The file brought its scanning array (points, pitch, angle, selected
            # point) and pixel size: show them, or the forms keep the old numbers
            # and the next "Apply settings" would send those back.
            self.ctrl_get_config_into_cfg()
            for group in ("scanning", "image"):
                self._sync_form(group)
            self._sync_size_fields()
            self.sp_ix.setValue(self.cfg.scanning.selected_index_x)
            self.sp_iy.setValue(self.cfg.scanning.selected_index_y)
            self.view.clear_scan_rect()           # a drawn rectangle belongs to the old array
            sc = self.cfg.scanning
            self._log_event("info", f"pattern array: {sc.points_x} x {sc.points_y} points, "
                                    f"pitch ({sc.dx_um:.3f}, {sc.dy_um:.3f}) um, "
                                    f"angle {sc.angle_deg:.2f} deg")

    def _save_pattern(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save pattern", "pattern.png", "PNG (*.png)")
        if path:
            try:
                self.ctrl.save_pattern(path)
            except Exception as exc:
                self._log_event("error", f"save failed: {exc}")

    # -- refresh loop ------------------------------------------------------ #
    def _frame(self):
        if hasattr(self.ctrl, "latest_frame"):
            return self.ctrl.latest_frame()
        try:
            return self.ctrl.get_frame()
        except Exception:
            return None

    def _refresh(self):
        s = self.ctrl.status()
        # keep cfg mirror's live fields in step so overlays are correct
        self.cfg.image.pixel_size_x_um = s.pixel_size_x
        self.cfg.image.pixel_size_y_um = s.pixel_size_y
        self.cfg.scanning.selected_index_x = s.selected_index_x
        self.cfg.scanning.selected_index_y = s.selected_index_y

        self.spot_tab.record(s)              # spot-area trace keeps running on every tab
        if self.tabs.currentWidget() is self.spot_page:
            self.spot_tab.update_status(s)   # numbers only: that tab works on a snapshot
        else:
            self.view.set_frame(self._frame())
            self.view.set_overlay(s, self.cfg)

        _set_led(self.led_match, s.match_found, T.OK)
        if s.backups_n:
            drv = "main" if s.pattern_driver == 0 else f"backup {s.pattern_driver}"
            self.lab_patterns.setText(f"{s.backups_n} backup(s) - driving: {drv}")
        else:
            self.lab_patterns.setText("no backups")
        _set_led(self.led_stable, s.stable, T.OK)
        _set_led(self.led_af, not s.af_running and s.af_error == "OK", T.OK)
        self.lab_af.setText("run" if s.af_running else s.af_error)
        # Z follows the Z device: volts on the piezo rig, um on the KIM rig,
        # whose range (kim's leash) can change while we run.
        if s.z_unit != self._z_unit:
            self._z_unit = s.z_unit
            self.z_spin.setSuffix(f" {s.z_unit}")
            self.z_step.setSuffix(f" {s.z_unit}")
            self.af_plot._xlabel = f"Z ({s.z_unit})"
            self._label_z_fields(s.z_unit)
        if (s.z_min, s.z_max) != (self.z_spin.minimum(), self.z_spin.maximum()):
            self.z_spin.setRange(s.z_min, s.z_max)
        self.lab_best.setText(f"{s.best_focus_v:.2f} {s.z_unit}")
        self._refresh_xy(s)
        self.lab_pxsize.setText(f"pixel: {s.pixel_size_x:.4f} um | obj: {s.objective_name}")
        self.lab_z.setText(f"at {s.z_voltage:.2f} {s.z_unit}")
        if not self._z_target_synced and s.connected:
            self.z_spin.setValue(s.z_voltage)   # start from where Z is, once
            self._z_target_synced = True

        for n, lab in getattr(self, "_ro", {}).items():
            val = getattr(s, n, "-")
            lab.setText(f"{val:.3f}" if isinstance(val, float) else str(val))

        # the focus plot fills in during a sweep (twice a second) and gets its
        # final curve + best-focus line when the sweep finishes
        if s.af_running:
            self._af_plot_tick = getattr(self, "_af_plot_tick", 0) + 1
            if self._af_plot_tick % 8 == 0:
                self._update_af_plot()
        if self._af_was_running and not s.af_running:
            self._update_af_plot()
        self._af_was_running = s.af_running

        # live accuracy trace while logging
        if getattr(self, "chk_acc", None) is not None and self.chk_acc.isChecked():
            try:
                acc = self.ctrl.get_accuracy()
            except Exception:
                acc = None
            if acc and acc["dx"]:
                idx = list(range(len(acc["dx"])))
                self.acc_plot.set_series([
                    (idx, acc["dx"], T.ACCENT_HI, "dX"),
                    (idx, acc["dy"], T.OK, "dY"),
                ])
                import math
                rms = lambda a: math.sqrt(sum(v * v for v in a) / len(a)) if a else 0.0
                self.lab_acc.setText(f"dX rms: {rms(acc['dx']):.3f} um   "
                                     f"dY rms: {rms(acc['dy']):.3f} um   "
                                     f"({len(acc['dx'])} samples)")

    def _log_event(self, level, msg):
        color = {"info": T.ACCENT_HI, "warn": T.ACCENT, "error": T.DANGER}.get(level, T.TEXT)
        self.log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    # Select the active palette BEFORE any widget is built, so the whole app
    # (stylesheet, Fusion palette, and every painted widget) uses one theme.
    T.set_theme(getattr(cfg.ui, "theme", "dark"))
    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (docs/DEVELOPER_NOTES.md gotcha #18): the XY jog step showed as "2,000 um".
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.OmitGroupSeparator)
    QLocale.setDefault(loc)
    app.setStyle("Fusion")
    T.apply_palette(app)
    app.setStyleSheet(T.build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()
