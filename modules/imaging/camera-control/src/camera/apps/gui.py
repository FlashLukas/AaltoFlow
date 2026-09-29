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

import html

import math
from dataclasses import fields

from PySide6.QtCore import QEvent, QLocale, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from . import theme as T
from .camera_view import CameraView
from .control_bar import ControlBar, mark_always
from .plots import MiniPlot
from .spot_tab import SAVE_CONFIG_TIP, SpotTab, sizes_summary
from .. import vision as V
from ..config import (AF_ROUTINES, AF_SIDES, CALIB_MODES, CLIP_MODES, DRIVERS,
                      FOCUS_MECHANISMS, LOCATE_MODES, MOTIONS, SIM_SPOTS, SIZE_METHODS,
                      SYMMETRIES, THEMES, XY_UNITS)

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
    "size_method": SIZE_METHODS,
    "clip_mode": CLIP_MODES,
    "sim_spot_model": SIM_SPOTS,
    # 2026-09-29: never free text where the choices are fixed
    "driver": DRIVERS,
    "locate": LOCATE_MODES,
    "calib_mode": CALIB_MODES,
    "motion": MOTIONS,
}

# What the focus plot's y axis shows, per autofocus mechanism.
AF_METRIC_LABELS = {"spot_area": "spot area (px²)",
                    "spot_d4sigma": "spot σ² (px²)",
                    "spot_relative": "relative area (px²)",
                    "spot_encircled": "encircled r86² (px²)",
                    "spot_gauss": "Gaussian fit σ² (px²)",
                    "spot_peak": "spot peak (counts)",
                    "edges": "edge sharpness", "fft": "high-frequency share"}

# --------------------------------------------------------------------------- #
# THREE-COLUMN settings layouts (2026-09-29, Lukas's screenshots: one long
# single-column form with full-width fields). Each tab = columns of compact
# group boxes; each box = (title, [(config group, field), ...]). A field of the
# tab's groups that no box names still appears, in a "More" box at the end --
# a new config field is never silently hidden.
# --------------------------------------------------------------------------- #
# The spot knobs that belong to each autofocus METRIC: shown in the AutoFocus
# tab only for the metric chosen there (one config source of truth: they are
# the Spot group's fields, so the live sizes use the same values).
_MOMENT_KNOBS = ["clip_mode", "clip_sigma", "detect_px", "min_blob_px", "mask_grow_px",
                 "box_factor", "max_iter", "smooth_px", "reject_asymmetric"]
MECH_KNOBS = {
    "spot_area": ["thr_lower", "thr_upper", "min_area_px", "max_area_px", "symmetric"],
    "spot_relative": ["rel_level", "clip_sigma", "smooth_px", "reject_asymmetric"],
    "spot_d4sigma": _MOMENT_KNOBS,
    "spot_encircled": ["encircled_fraction"] + _MOMENT_KNOBS,
    "spot_gauss": _MOMENT_KNOBS,                 # the fit starts from the moments
    "spot_peak": ["clip_sigma", "reject_asymmetric"],
    "edges": [], "fft": [],
}
_SPOT_KNOBS = []
for _v in MECH_KNOBS.values():
    for _k in _v:
        if _k not in _SPOT_KNOBS:
            _SPOT_KNOBS.append(_k)

_A = "autofocus"
# FOUR columns (Lukas, 2026-09-29, screenshot: with three the third column --
# Park + Scan + Z step calibration -- was the tallest and its bottom rows were
# cut off on the lab screen: "four panels then... I want to see what is
# below"). Balanced by height: the metric (its knobs vary with the metric) /
# the routine / park + the AF exposure / scan + Z step calibration.
AF_LAYOUT = [
    [("Metric", [(_A, "mechanism")] + [("spot", k) for k in _SPOT_KNOBS]
      + [(_A, "focus_from_safety_area"), (_A, "averages_per_level"),
         (_A, "zoom_on_af")])],
    [("Routine", [(_A, "routine"), (_A, "approach_from"), (_A, "approach_margin"),
                  (_A, "fit_curve")]),
     ("Sweep", [(_A, "drive_amplitude_v"), (_A, "steps")]),
     ("One way", [(_A, "coarse_step_v"), (_A, "fine_step_v"), (_A, "max_travel_v"),
                  (_A, "rise_fraction"), (_A, "rise_levels")])],
    [("Park", [(_A, "park_tolerance"), (_A, "park_tolerance_d4sigma"),
               (_A, "park_tolerance_relative"), (_A, "park_noise_k"), (_A, "park_centre"),
               (_A, "offset_from_found_v")]),
     ("Autofocus exposure", [(_A, "exposure_us"), (_A, "exposure_discard_frames")])],
    [("Scan & continuous", [(_A, "scan_timeout_s"), (_A, "continuous_enabled"),
                            (_A, "continuous_gain"), (_A, "continuous_target")]),
     ("Z step calibration", [(_A, "zcal_step_v"), (_A, "zcal_start_offset_v"),
                             (_A, "zcal_max_travel_v"), (_A, "zcal_averages"),
                             (_A, "zcal_width_levels"), (_A, "zcal_width_max_spread"),
                             (_A, "zcal_width_min_levels"), (_A, "zcal_walk_to"),
                             (_A, "zcal_skip_first"), (_A, "zcal_fit_window"),
                             (_A, "zcal_fit_min_r2"), (_A, "zcal_min_side_levels"),
                             (_A, "zcal_step_um")])],
]
# which routine (or switch) uses which autofocus field; the rest of the tab is
# used by both. Unused fields are GREYED (values kept), with a tooltip.
SWEEP_ONLY = {"drive_amplitude_v", "steps"}
ONE_WAY_ONLY = {"approach_from", "coarse_step_v", "fine_step_v", "max_travel_v",
                "rise_fraction", "rise_levels", "park_tolerance", "park_tolerance_d4sigma",
                "park_tolerance_relative", "park_noise_k", "park_centre"}
CAMERA_LAYOUT = [
    [("Objective & pixels", [("image", "objective_name"), ("image", "pixel_size_x_um"),
                             ("image", "pixel_size_y_um"), ("image", "objectives_file")])],
    [("Image geometry", [("image", "rotation_deg"), ("image", "symmetry"),
                         ("image", "clip_enabled"), ("image", "clip_left"),
                         ("image", "clip_top"), ("image", "clip_right"),
                         ("image", "clip_bottom")])],
    [("Camera", [("camera", "driver"), ("camera", "exposure_us"),
                 ("camera", "frame_rate"), ("camera", "running_avg_frames"),
                 ("camera", "extra_delay_ms"), ("camera", "video_mode"),
                 ("camera", "camera_name"), ("image", "save_path")]),
     ("Auto exposure (once)", [("camera", "auto_exposure_target"),
                               ("camera", "auto_exposure_percentile"),
                               ("camera", "auto_exposure_iterations")]),
     ("Simulator", [("camera", "sim_spot_model"), ("camera", "sim_bit_depth")])],
]
# --------------------------------------------------------------------------- #
# ZOOM TO THE SPOT REGION (2026-09-29, Lukas: "add an option in autofocus that
# when you call autofocus the image will zoom to the spot detection area").
# The region is the spot SEARCH region (Spot tab: lookup_region_px /
# lookup_region_y_px / search_shape) around the calibrated laser -- the same
# geometry every spot search uses (vision.search_region) -- plus a margin so
# the region's own dotted outline stays visible. Before the first calibration
# the searches look around the frame CENTRE, so the zoom does too.
ZOOM_MARGIN_FRACTION = 0.15        # of the region's half-size, per side
ZOOM_MARGIN_MIN_PX = 10
AF_ZOOM_NOTE = ("zoomed to the spot region (autofocus) -- "
                "double-click to show the whole frame")
ZOOM_IN_TEXT = "Zoom to spot region"
ZOOM_OUT_TEXT = "Whole frame"


def spot_zoom_rect(status, spot_cfg, frame_w: int, frame_h: int):
    """(x0, y0, x1, y1) in image px: the spot search region around the
    calibrated spot (frame centre when not calibrated) + a margin, clipped to
    the frame. None if there is no region (cannot happen with the clamp in
    config.Spot, but a zoom must never be guessed)."""
    if getattr(status, "spot_calibrated", False):
        centre = (float(status.spot_x), float(status.spot_y))
    else:
        centre = (frame_w / 2.0, frame_h / 2.0)
    reg = V.search_region(centre, spot_cfg.lookup_region_px, spot_cfg.lookup_region_y_px,
                          spot_cfg.search_shape)
    if reg is None:
        return None
    hx, hy = reg["half"]
    mx = max(ZOOM_MARGIN_MIN_PX, ZOOM_MARGIN_FRACTION * hx)
    my = max(ZOOM_MARGIN_MIN_PX, ZOOM_MARGIN_FRACTION * hy)
    x0, y0, x1, y1 = reg["box"]
    x0, y0 = max(0.0, x0 - mx), max(0.0, y0 - my)
    x1, y1 = min(float(frame_w), x1 + mx), min(float(frame_h), y1 + my)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return (x0, y0, x1, y1)


_P = "pattern"
PATTERN_LAYOUT = [
    [("Matching", [(_P, "n_matches"), (_P, "min_match_score"), (_P, "angle_start"),
                   (_P, "angle_end"), (_P, "angle_step"), (_P, "safety_area_px"),
                   (_P, "full_image")])],
    [("Backup patterns", [(_P, "edge_margin_px"), (_P, "offset_learn_rate"),
                          (_P, "offset_warn_px")])],
    [("Losing the pattern", [(_P, "lost_frames"), (_P, "loss_edge_margin_px"),
                             (_P, "loss_spot_margin_px"), (_P, "autofocus_on_loss"),
                             (_P, "recovery_min_interval_s")])],
]
_H = "hardware"
POSITIONER_LAYOUT = [
    [("Limits", [("limits", k) for k in ("motor_x_min", "motor_x_max", "motor_y_min",
                                        "motor_y_max", "z_min_v", "z_max_v", "enforce")])],
    [("Rig", [(_H, "motion"), (_H, "xy_unit"), (_H, "kim_host"), (_H, "kim_cmd_port"),
              (_H, "kim_pub_port"), (_H, "use_z"), (_H, "z_step_v"), (_H, "z_step_time_ms"),
              (_H, "cam_device")])],
    [("Piezo / Z services", [(_H, "use_remote_xy"), (_H, "piezo_host"),
                             (_H, "piezo_cmd_port"), (_H, "piezo_pub_port"),
                             (_H, "use_remote_z"), (_H, "z_host"), (_H, "z_cmd_port"),
                             (_H, "z_pub_port"), (_H, "kcube_serial")])],
]
# field widths: numbers are short, paths and hosts are not (no 1900-px boxes)
_NUM_W = 110
# A settings LABEL is at most this wide; a longer field name wraps at its
# underscores ("park_tolerance_" / "relative"). Four autofocus columns of
# unwrapped names were ~1220 px wide -- wider than the lab screen's 1080 px
# content area, so the last column was off to the right (2026-09-29). The
# AutoFocus tab uses the narrow width; the other tabs have room for more.
_LABEL_W = 130
_LABEL_W_AF = 100
_TEXT_W = 170
_PATH_W = 220


def _size_widget(name: str, w) -> None:
    """Size an editing widget to its content, not to the window."""
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        w.setMaximumWidth(_NUM_W)
    elif isinstance(w, QLineEdit):
        wide = any(k in name for k in ("path", "file", "dir"))
        w.setMinimumWidth(_PATH_W if wide else 90)
        w.setMaximumWidth(_PATH_W + 160 if wide else _TEXT_W)
    elif isinstance(w, QComboBox):
        w.setSizeAdjustPolicy(QComboBox.AdjustToContents)


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


def _set_label(lab: QLabel, text: str, width: int = _LABEL_W) -> None:
    """A settings label no wider than ``width``: a name that fits stays on one
    line; a longer one wraps at its underscores / spaces (a zero-width space
    after each "_" marks where it may break). The minimum width is the text's
    own (up to ``width``), so the layout never wraps a name that fits."""
    lab.setText(text.replace("_", "_​"))
    lab.setWordWrap(True)
    lab.ensurePolished()          # the stylesheet's font, not the default one
    one_line = lab.fontMetrics().horizontalAdvance(text) + 4
    lab.setMinimumWidth(min(one_line, width))
    lab.setMaximumWidth(width)


def _sized_widget_for_field(name: str, value):
    w, get = _widget_for_field(name, value)
    _size_widget(name, w)
    return w, get


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
COMPACT_COLS = 4          # label/field pairs per row in a compact settings form


class _WheelToScroll(QObject):
    """Mouse wheel over a number box or a combo SCROLLS THE PAGE unless that
    box has the keyboard focus (click into it first to wheel its value).

    Qt's default gives the wheel to whatever spin box / combo is under the
    pointer: scrolling down a long settings tab silently changed values and the
    page did not move -- so what was below looked unreachable (Lukas,
    2026-09-29: "I want to see what is below"). The event is handed to the
    enclosing scroll area's vertical scroll bar instead.
    """

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel and not obj.hasFocus():
            area = obj.parent()
            while area is not None and not isinstance(area, QScrollArea):
                area = area.parent()
            if area is not None:
                QApplication.sendEvent(area.verticalScrollBar(), ev)
            return True           # never to the box itself
        return False


_WHEEL_GUARD = None


def _guard_wheel(widget: QWidget) -> None:
    global _WHEEL_GUARD
    if isinstance(widget, (QAbstractSpinBox, QComboBox)):
        if _WHEEL_GUARD is None:
            _WHEEL_GUARD = _WheelToScroll()
        widget.setFocusPolicy(Qt.StrongFocus)     # focus by click / Tab, not by wheel
        widget.installEventFilter(_WHEEL_GUARD)


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
        # group -> {field: its QLabel} (the three-column layouts have no
        # QFormLayout to ask), and the Apply payload's groups per tab
        self._form_labels: dict[str, dict] = {}
        # group -> {field: the value the form last SHOWED (built or synced)}.
        # "Apply settings" sends only fields whose widget differs from this,
        # i.e. what the user actually edited -- see _apply_settings.
        self._form_shown: dict[str, dict] = {}
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
        self.view.unzoom_requested.connect(self._dismiss_af_zoom)
        self._af_was_running = False
        # Zoom state (see _refresh_zoom). _zoom_spot_on = the user's own
        # "Zoom to spot region" toggle; the autofocus zoom is laid OVER it and
        # the view the user had is put back when the run ends.
        self._zoom_spot_on = False
        self._af_zoom_active = False     # the autofocus zoom is applied now
        self._af_zoom_dismissed = False  # the user un-zoomed during this run
        self._zoom_saved = None          # the view (zoom rect) before the run
        self._zoom_run_was = False       # an AF / Z calibration ran last refresh
        self._zoom_run_key = None        # (af_id, zcal_id) of that run

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
        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

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
        self.af_page = _scrolled(self._autofocus_tab())
        tabs.addTab(self.af_page, "AutoFocus")
        tabs.addTab(_scrolled(self._settings_tab([("Pattern", self.cfg.pattern)],
                                                 columns=PATTERN_LAYOUT)), "Pattern")
        tabs.addTab(self._camera_settings_tab(), "Camera settings")   # scrolls already
        tabs.addTab(_scrolled(self._settings_tab([("Limits", self.cfg.limits),
                                                  ("Hardware", self.cfg.hardware)],
                                                 columns=POSITIONER_LAYOUT)), "Positioner")
        tabs.addTab(_scrolled(self._readouts_tab()), "Readouts")
        tabs.addTab(_scrolled(self._appearance_tab()), "Appearance")
        # a tab shown again re-reads the config into its (unedited) form: the
        # AutoFocus tab shows Spot fields the Spot tab may have changed
        tabs.currentChanged.connect(self._on_tab_changed)

        central = QWidget(); root = QVBoxLayout(central)
        # Control or viewer (control_bar.py), only for a GUI on a service
        # whose client knows about control; a local GUI owns its brain.
        self._control_bar = None
        if self.remote and hasattr(self.ctrl, "take_control"):
            self._control_bar = ControlBar(self.ctrl, self, log=self._log_event)
            root.addWidget(self._control_bar)
        root.addWidget(tabs, 1)
        self.log = QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumHeight(120)
        root.addWidget(self.log)
        self.setCentralWidget(central)
        self._label_z_fields(self._z_unit)

    def _camera_tab(self) -> QWidget:
        w = QWidget(); lay = QHBoxLayout(w)
        # The controls sit in TWO columns beside the image, not one. In one
        # column they were ~800 px tall and pushed the bottom tabs ("Define
        # scanning", ...) below the screen, so the tab had to be scrolled to
        # reach them (Lukas, 2026-09-25). Two columns halve that height; the
        # image gives up the width, and keeps its aspect as it shrinks.
        # the view with, under it, the zoom toggle (Lukas 2026-09-29): the same
        # zoom the autofocus uses, useful outside autofocus too
        left = QWidget(); lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(4)
        lv.addWidget(self.view, 1)
        zr = QHBoxLayout()
        self.b_zoom = QPushButton(ZOOM_IN_TEXT)
        self.b_zoom.setToolTip("Show only the spot search region around the calibrated "
                               "laser (Spot tab: search region), or the whole frame again. "
                               "During an autofocus zoom it shows the whole frame for the "
                               "rest of that run.")
        self.b_zoom.clicked.connect(self._toggle_zoom)
        mark_always(self.b_zoom)     # the view only: fine for a viewer
        zr.addWidget(self.b_zoom); zr.addStretch(1)
        lv.addLayout(zr)
        lay.addWidget(left, 4)
        cards: list[QWidget] = []         # Focus, Pattern, Stabiliser, Imaging

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
        self.b_setz = QPushButton("Set")
        self.b_setz.clicked.connect(lambda: self.ctrl.set_z(self.z_spin.value()))
        row.addWidget(self.b_setz)
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
        # Datum Z (Lukas, 2026-09-29): he re-zeroes Z at every focus the image
        # confirmed. Next to the Z controls; danger style + a confirm, since it
        # redefines every Z noted before. Enabled only on a Z with a counter.
        self.b_datum_z = QPushButton("Datum Z"); self.b_datum_z.setObjectName("danger")
        self.b_datum_z.clicked.connect(self._datum_z)
        row.addWidget(self.b_datum_z)
        l.addLayout(row)
        row2 = QHBoxLayout()
        self.b_af = QPushButton("Find focus"); self.b_af.setObjectName("primary")
        self.b_af.clicked.connect(lambda: self.ctrl.autofocus())
        b_kill = QPushButton("Kill AF"); b_kill.setObjectName("danger")
        b_kill.clicked.connect(lambda: self.ctrl.kill_af())
        mark_always(b_kill)          # a viewer can always stop an autofocus
        row2.addWidget(self.b_af); row2.addWidget(b_kill); l.addLayout(row2)
        r3 = QHBoxLayout(); r3.addWidget(QLabel("AF")); self.led_af = _led(T.OK)
        r3.addWidget(self.led_af); self.lab_af = QLabel("OK"); r3.addWidget(self.lab_af)
        r3.addStretch(1); r3.addWidget(QLabel("Best")); self.lab_best = QLabel("0.00 V")
        r3.addWidget(self.lab_best); l.addLayout(r3)
        self.chk_cont = QCheckBox("Continuous focus")
        self.chk_cont.toggled.connect(lambda v: self.ctrl.set_continuous_focus(v))
        l.addWidget(self.chk_cont)
        cards.append(f)

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
        cards.append(f)

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
        cards.append(f)

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
        cards.append(f)
        right_w = QWidget(); grid = QGridLayout(right_w)
        grid.setContentsMargins(0, 0, 0, 0)
        # The FAULT banner goes first, above everything: a lost pattern stops
        # the stabiliser and pauses a scan, so it must be the first thing seen.
        grid.addWidget(self._fault_bar(), 0, 0, 1, 2)
        grid.addWidget(self._stage_bar(), 1, 0, 1, 2)
        # column 1: what you do to the SAMPLE's image (focus, pattern);
        # column 2: what runs on it (stabiliser) and what is drawn (imaging).
        for (r, c), card in zip(((2, 0), (3, 0), (2, 1), (3, 1)), cards):
            grid.addWidget(card, r, c)
        grid.setRowStretch(4, 1)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        lay.addWidget(right_w, 5)

        # bottom sub-tabs
        sub = QTabWidget()
        sub.addTab(self._xy_subtab(), "Control XY stage")
        sub.addTab(self._scanning_subtab(), "Define scanning")
        sub.addTab(self._accuracy_subtab(), "Check alignment accuracy")
        outer = QWidget(); ov = QVBoxLayout(outer)
        ov.addWidget(w, 3); ov.addWidget(sub, 2)
        return outer

    def _laser_card(self) -> QWidget:
        """Where the laser is ON THE SAMPLE, and putting it somewhere.

        X / Y = the laser spot measured from the MAIN template, in um, every
        frame (image +x right, +y down) -- the sample's own coordinates, so a
        slipping open-loop stage does not enter. Place moves the sample until
        the laser is at the target (closed loop on the image, like the
        stabiliser, which it switches off: one stage, one target). The same
        numbers are `camera.laser_x/y` in scans, and what a fly scan in camera
        coordinates bins by.
        """
        f, l = _card("Laser on sample")
        f.setFixedWidth(330)
        r = QHBoxLayout()
        self.lab_laser = QLabel("x -   y -  um")
        self.lab_laser.setStyleSheet("font-weight:700;")
        self.lab_laser.setToolTip("laser spot measured from the main template, um "
                                  "(image +x right, +y down)")
        r.addWidget(self.lab_laser, 1)
        r.addWidget(QLabel("Placed")); self.led_laser = _led(T.OK)
        r.addWidget(self.led_laser)
        l.addLayout(r)
        r = QHBoxLayout()
        self.laser_tx = QDoubleSpinBox(); self.laser_ty = QDoubleSpinBox()
        for sp, name in ((self.laser_tx, "X"), (self.laser_ty, "Y")):
            sp.setRange(-10000.0, 10000.0); sp.setDecimals(3); sp.setSingleStep(0.5)
            sp.setSuffix(" um")
            r.addWidget(QLabel(name)); r.addWidget(sp)
        l.addLayout(r)
        r = QHBoxLayout()
        self.b_laser_here = QPushButton("Here")
        self.b_laser_here.setToolTip("fill the target with where the laser is now")
        self.b_laser_here.clicked.connect(self._laser_here)
        self.b_laser_place = QPushButton("Place"); self.b_laser_place.setObjectName("primary")
        self.b_laser_place.setToolTip("move the sample until the laser is at the target "
                                      "(switches the stabiliser off)")
        self.b_laser_place.clicked.connect(self._laser_place)
        self.b_laser_cancel = QPushButton("Cancel")
        self.b_laser_cancel.clicked.connect(self._laser_cancel)
        mark_always(self.b_laser_cancel)
        r.addWidget(self.b_laser_here); r.addWidget(self.b_laser_place)
        r.addWidget(self.b_laser_cancel); l.addLayout(r)
        self.lab_laser_state = QLabel("no target"); self.lab_laser_state.setObjectName("muted")
        l.addWidget(self.lab_laser_state)
        return f

    def _laser_here(self) -> None:
        s = self.ctrl.status()
        x, y = s.spot_from_template_x_um, s.spot_from_template_y_um
        if x == x and y == y:                  # not NaN
            self.laser_tx.setValue(x); self.laser_ty.setValue(y)
        else:
            self._log_event("warn", "laser position unknown: needs a tracked pattern "
                                    "and a calibrated spot")

    def _laser_place(self) -> None:
        try:
            t = self.ctrl.set_laser_target(self.laser_tx.value(), self.laser_ty.value())
            self._log_event("info", f"placing the laser at x {t[0]:.3f}, y {t[1]:.3f} um")
        except Exception as exc:
            self._log_event("warn", f"cannot place the laser: {exc}")

    def _laser_cancel(self) -> None:
        try:
            self.ctrl.cancel_laser_target()
        except Exception as exc:
            self._log_event("warn", f"cancel failed: {exc}")

    def _refresh_laser(self, s) -> None:
        x, y = s.spot_from_template_x_um, s.spot_from_template_y_um
        known = x == x and y == y
        self.lab_laser.setText(f"x {x:.3f}   y {y:.3f}  um" if known else "x -   y -  um")
        _set_led(self.led_laser, bool(getattr(s, "laser_settled", False)), T.OK)
        tx = getattr(s, "laser_target_x_um", float("nan"))
        if getattr(s, "streaming", False):
            text = "a fly scan is recording -- placement stands down"
        elif getattr(s, "laser_goto", False):
            text = f"placing ... {s.distance_um:.3f} um to go"
        elif tx != tx:
            text = "no target"
        elif getattr(s, "laser_settled", False):
            text = "at the target"
        else:
            text = (f"target x {tx:.3f}, y {s.laser_target_y_um:.3f} um -- "
                    f"not there (stopped or moved away)")
        self.lab_laser_state.setText(text)
        # Without a matched pattern there ARE no sample coordinates: the whole
        # card is greyed, and the state line says what is missing (Cancel
        # stays usable while a placement is still running).
        ready = bool(s.match_found and s.spot_calibrated and s.tracking_on)
        stage = bool(getattr(s, "stage_ok", True))
        if not ready:
            missing = [what for ok, what in ((s.tracking_on, "tracking on"),
                                             (s.match_found, "a matched pattern"),
                                             (s.spot_calibrated, "a calibrated spot")) if not ok]
            self.lab_laser_state.setText("needs " + ", ".join(missing))
        for w_ in (self.laser_tx, self.laser_ty, self.lab_laser, self.b_laser_here):
            w_.setEnabled(ready)
        self.b_laser_here.setEnabled(ready and known)
        self.b_laser_place.setEnabled(ready and stage)
        self.b_laser_cancel.setEnabled(bool(getattr(s, "laser_goto", False)) or (ready and stage))
        self.b_laser_place.setToolTip(
            "move the sample until the laser is at the target (switches the "
            "stabiliser off)" if ready else
            "needs: pattern tracking on, the pattern matched, a calibrated spot")

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
        note = QLabel("The autofocus METRIC is chosen here, with the knobs that belong to "
                      "it (shown for the chosen metric only; they are the Spot settings, so "
                      "the live sizes use the same values). spot_d4sigma = the spot's second "
                      "moment σ²: no threshold, rings and a hole counted where they are, a "
                      "parabola in Z. Settings the chosen routine does not use are greyed.")
        note.setObjectName("muted"); note.setWordWrap(True)
        v.addWidget(note)
        v.addWidget(self._settings_tab([("Autofocus", self.cfg.autofocus),
                                        ("Spot", self.cfg.spot)], columns=AF_LAYOUT,
                                       partial=("spot",), save=True,
                                       label_w=_LABEL_W_AF))
        self.lab_af_expo = QLabel("")
        self.lab_af_expo.setVisible(False)
        v.addWidget(self.lab_af_expo)
        afw = self._form_widgets["autofocus"]
        afw["routine"].currentTextChanged.connect(self._af_rules)
        afw["mechanism"].currentTextChanged.connect(self._af_rules)
        afw["continuous_enabled"].toggled.connect(self._af_rules)
        self._af_rules()
        # the three spot sizes of the current frame, from status (no image work):
        # watch them while focusing by hand to see which one behaves
        f, l = _card("Spot size now")
        self.lab_af_sizes = QLabel("-"); self.lab_af_sizes.setWordWrap(True)
        self.lab_af_sizes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.lab_af_sizes)
        v.addWidget(f)
        f, l = _card("Focus sweep (metric vs Z voltage)")
        self.af_plot = MiniPlot(xlabel="Z (V)", ylabel="metric")
        self.af_plot.setMinimumHeight(170)
        l.addWidget(self.af_plot)
        row = QHBoxLayout()
        b = QPushButton("Show last sweep"); b.clicked.connect(self._update_af_plot)
        row.addWidget(b); mark_always(b)        # only reads: fine for a viewer
        b = QPushButton("Show Z calibration"); b.clicked.connect(self._update_zcal_plot)
        row.addWidget(b); mark_always(b)
        row.addStretch(1)
        l.addLayout(row)
        v.addWidget(f)
        # Z STEP CALIBRATION (2026-09-28): on the open-loop kim Z a step up is
        # not a step down, so "go back to the best Z" by the counter misses.
        # The camera measures the two step sizes from the spot's sigma^2.
        f, l = _card("Z step calibration (open-loop Z)")
        # 2026-09-29: the ratio comes from the curves' WIDTHS at equal sigma^2
        # levels (the rig's walk was lopsided: the step size varies along a
        # walk, so one parabola per walk was the wrong model)
        note = QLabel("With the spot calibrated and roughly in focus: walks Z up through "
                      "focus, then down, measuring σ² (D4σ) at equal counter steps. At "
                      "equal σ² levels the down walk's counter width / the up walk's is the "
                      "step ratio up/down (no parabola assumed); the levels must agree. "
                      "Both step sizes go to kim (their geometric mean kept). Refuses rather "
                      "than guess. The sweep routine relies on it on this Z. Kill AF stops "
                      "it. Settings: zcal_* above.")
        note.setObjectName("muted"); note.setWordWrap(True)
        l.addWidget(note)
        row = QHBoxLayout()
        self.b_zcal = QPushButton("Calibrate Z steps"); self.b_zcal.setObjectName("primary")
        self.b_zcal.clicked.connect(self._calibrate_z_steps)
        row.addWidget(self.b_zcal); row.addStretch(1)
        l.addLayout(row)
        self.lab_zcal = QLabel("not run yet"); self.lab_zcal.setWordWrap(True)
        self.lab_zcal.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.lab_zcal)
        v.addWidget(f)
        return w

    def _af_rules(self, *_):
        """Grey what the chosen routine does not use; show only the chosen
        metric's knobs. Follows the COMBOS (before Apply), so what you see is
        what the next Apply will run. Values are kept, never cleared."""
        afw = self._form_widgets.get("autofocus", {})
        afl = self._form_labels.get("autofocus", {})
        if "routine" not in afw:
            return
        routine = afw["routine"].currentText()
        cont = afw["continuous_enabled"].isChecked()
        for name, wdg in afw.items():
            why = ""
            if routine == "sweep" and name in ONE_WAY_ONLY:
                why = "not used by routine sweep"
            elif routine == "one_way" and name in SWEEP_ONLY:
                why = "not used by routine one_way"
            elif name == "continuous_gain" and not cont:
                why = "only with continuous_enabled"
            elif name == "continuous_target":
                why = "not used (see the config comment)"
            for x in (wdg, afl.get(name)):
                if x is not None:
                    x.setEnabled(not why)
                    x.setToolTip(why)
        mech = afw["mechanism"].currentText()
        knobs = set(MECH_KNOBS.get(mech, []))
        spw = self._form_widgets.get("spot", {})
        spl = self._form_labels.get("spot", {})
        for name in _SPOT_KNOBS:
            for x in (spw.get(name), spl.get(name)):
                if x is not None:
                    x.setVisible(name in knobs)

    def _calibrate_z_steps(self):
        try:
            self.ctrl.calibrate_z_steps()
        except Exception as exc:
            self._log_event("error", f"Z step calibration: {exc}")

    def _refresh_zcal(self, s) -> None:
        lab = getattr(self, "lab_zcal", None)
        if lab is None:
            return
        running = bool(getattr(s, "zcal_running", False))
        self.b_zcal.setEnabled(not running and not s.af_running)
        state = getattr(s, "zcal_state", "OK")
        if running:
            lab.setText(f"#{s.zcal_id}: {state} ...")
            return
        if not getattr(s, "zcal_id", 0):
            lab.setText("not run yet")
            return
        q = getattr(s, "zcal_ratio", float("nan"))
        levels = getattr(s, "zcal_levels", "")
        spread = getattr(s, "zcal_ratio_spread", float("nan"))
        q_fit = getattr(s, "zcal_ratio_fit", float("nan"))
        # the evidence, shown for a result AND for a refusal: the per-level
        # width ratios and their spread, then the parabola fits as diagnostics
        evidence = ""
        if levels:
            evidence += (f"<br>per σ² level (× minimum): {levels} &nbsp; spread "
                         f"{100 * spread:.1f} %")
        if math.isfinite(q_fit):
            evidence += (f"<br><span style='color:{T.COLORS['muted']}'>parabola fits "
                         f"(diagnostic): ratio {q_fit:.3f}, R² {s.zcal_r2_up:.4f} / "
                         f"{s.zcal_r2_down:.4f}</span>")
        # "Z left off focus ..." = the ratio WAS measured and written, but the
        # park could not bring Z back to focus by the image (rig 2026-09-29):
        # show the result AND the warning, not one of them
        off_focus = state.startswith("Z left off focus")
        if (state == "OK" or off_focus) and math.isfinite(q):
            warn = (f"<br><span style='color:{T.COLORS['danger']}'>{html.escape(state)}</span>"
                    if off_focus else "")
            lab.setText(f"#{s.zcal_id}: step up / down = <b>{q:.3f}</b> ± "
                        f"{s.zcal_ratio_err:.3f} (width method) &nbsp; up "
                        f"{s.zcal_up_um:.5g}, down {s.zcal_down_um:.5g} "
                        f"{self._z_unit}/step{warn}{evidence}")
        else:
            lab.setText(f"#{s.zcal_id}: <span style='color:{T.COLORS['danger']}'>"
                        f"{state}</span>{evidence}")

    def _update_zcal_plot(self):
        """The last Z step calibration's two walks: sigma^2 against the counter."""
        try:
            c = self.ctrl.get_zcal_curve()
        except Exception:
            return
        series = []
        for key, color, name in (("up", T.ACCENT_HI, "walk up"), ("down", T.OK, "walk down")):
            pts = [(z, m) for z, m in zip(c.get(key, {}).get("z", []),
                                          c.get(key, {}).get("metric", []))
                   if m is not None and math.isfinite(m)]
            if pts:
                series.append(([p[0] for p in pts], [p[1] for p in pts], color, name))
        # the WIDTH method made visible: each sigma^2 level as a dashed line,
        # and where each walk crossed it (open circles, the walk's colour).
        # The width of a walk at a level = the distance between its two circles.
        hlines, marks = [], []
        for lev in c.get("levels", []):
            hlines.append((lev.get("level"), T.MUTED))
            for key, color in (("up", T.ACCENT_HI), ("down", T.OK)):
                for x in lev.get(key, []):
                    marks.append((x, lev.get("level"), color))
        if series:
            self.af_plot._xlabel = f"Z counter ({c.get('unit', self._z_unit)})"
            self.af_plot._ylabel = "σ² (px²)"
            self.af_plot.set_series(series, hlines=hlines, marks=marks)

    # -- stage availability -------------------------------------------------
    # The stage (kim) is its own service and can be off or restarting. The
    # camera keeps imaging either way; what must not happen is a click that
    # waits out a network timeout, or a control that looks usable and is not.
    # So: every stage control is greyed out while the stage is not answering,
    # and a bar says so, with a button to reconnect (Lukáš, 2026-09-25).
    def _stage_bar(self) -> QWidget:
        bar = QFrame(); row = QHBoxLayout(bar); row.setContentsMargins(0, 2, 0, 2)
        lab = QLabel("stage: -"); lab.setWordWrap(True)
        btn = QPushButton("Reconnect stage")
        btn.setToolTip("Rebuild the connection to the stage service, e.g. after "
                       "starting or restarting it.")
        btn.clicked.connect(self._reconnect_stage)
        row.addWidget(lab, 1); row.addWidget(btn)
        self.__dict__.setdefault("_stage_bars", []).append((lab, btn))
        return bar

    def _fault_bar(self) -> QWidget:
        """Red banner: the latched FAULT (a lost pattern) and any hardware error.

        Hidden while both are empty. "Clear fault" is refused by the camera
        while the pattern is still not found; the refusal is shown in the log.
        """
        bar = QFrame(); bar.setObjectName("card")
        row = QHBoxLayout(bar); row.setContentsMargins(8, 4, 8, 4)
        self.lab_fault = QLabel(""); self.lab_fault.setWordWrap(True)
        self.b_clear_fault = QPushButton("Clear fault"); self.b_clear_fault.setObjectName("danger")
        self.b_clear_fault.setToolTip("Correct the cause first (focus, bring the pattern back "
                                      "into view, move the spot off it), then clear. The "
                                      "stabiliser holds the stage until then.")
        self.b_clear_fault.clicked.connect(self._clear_fault)
        row.addWidget(self.lab_fault, 1); row.addWidget(self.b_clear_fault)
        self.fault_bar = bar
        bar.setVisible(False)
        return bar

    def _clear_fault(self) -> None:
        try:
            self.ctrl.clear_fault()
        except Exception as exc:
            self._log_event("warn", f"clear fault refused: {exc}")

    def _sync_fault(self, s) -> None:
        fault = getattr(s, "fault", "") or ""
        hw = getattr(s, "hw_error", "") or ""
        lines = []
        if fault:
            lines.append(f"FAULT: {fault}")
        if hw:
            lines.append(f"HARDWARE: {hw}")
        self.fault_bar.setVisible(bool(lines))
        self.b_clear_fault.setVisible(bool(fault))
        if lines:
            self.lab_fault.setText(" | ".join(lines))
            self.lab_fault.setStyleSheet(f"color:{T.DANGER}; font-weight:700;")

    def _stage_controls(self) -> list:
        names = ("z_spin", "b_setz", "z_step", "b_z_up", "b_z_dn", "b_af", "chk_cont",
                 "chk_stab", "chk_click", "xy_step", "b_x_up", "b_x_dn", "b_y_up",
                 "b_y_dn", "sp_x", "sp_y", "b_move_abs")
        return [getattr(self, n) for n in names if getattr(self, n, None) is not None]

    def _sync_stage(self, s) -> None:
        ok = bool(getattr(s, "stage_ok", True))
        why = getattr(s, "stage_error", "") or "stage not answering"
        for w in self._stage_controls():
            w.setEnabled(ok)
        if getattr(self, "b_datum", None) is not None:
            self.b_datum.setEnabled(ok and s.xy_has_datum)
        if getattr(self, "b_datum_z", None) is not None:
            has = bool(getattr(s, "z_has_datum", False))
            busy = bool(getattr(s, "af_running", False) or getattr(s, "zcal_running", False))
            self.b_datum_z.setEnabled(ok and has and not busy)
            self.b_datum_z.setToolTip(
                "Set the Z step counter to 0 HERE -- nothing moves. Use it at a focus "
                "you trust." if has else
                "no datum: this Z has no step counter (only the kim Z has one)")
        for lab, btn in getattr(self, "_stage_bars", []):
            if ok:
                lab.setText("stage: connected")
                lab.setStyleSheet(f"color:{T.OK};")
            else:
                lab.setText(why)
                lab.setStyleSheet(f"color:{T.DANGER}; font-weight:600;")
            btn.setVisible(not ok)

    def _reconnect_stage(self) -> None:
        for _lab, btn in getattr(self, "_stage_bars", []):
            btn.setEnabled(False); btn.setText("Reconnecting...")
        QApplication.processEvents()
        try:
            res = self.ctrl.reconnect_stage()
            ok, why = res.get("stage_ok", True), res.get("stage_error", "")
            self._log_event("info" if ok else "warn",
                            "stage reconnected" if ok else f"stage reconnect failed: {why}")
        except Exception as exc:
            self._log_event("error", f"reconnect stage: {exc}")
        finally:
            for _lab, btn in getattr(self, "_stage_bars", []):
                btn.setEnabled(True); btn.setText("Reconnect stage")

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
        l.addWidget(self._stage_bar())
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
        self.b_move_abs = QPushButton("Move"); self.b_move_abs.setFixedWidth(BTN_W)
        self.b_move_abs.clicked.connect(self._move_xy_abs)
        g.addWidget(self.b_move_abs, 0, 2, 2, 1, Qt.AlignVCenter)
        l.addLayout(g)
        self.b_datum = QPushButton("Datum XY  (zero here)"); self.b_datum.setObjectName("danger")
        self.b_datum.setToolTip("Reset the stage's X and Y step counters to 0 at the "
                                "current position (kim Datum).")
        self.b_datum.clicked.connect(self._datum_xy)
        l.addWidget(self.b_datum)
        l.addStretch(1)
        lay.addWidget(f, 0, Qt.AlignTop)

        # -- the laser on the SAMPLE (template coordinates) -- next to the stage
        #    controls it belongs with; on the Camera tab it pushed the bottom
        #    tabs down again (Lukas, 2026-09-28)
        lay.addWidget(self._laser_card(), 0, Qt.AlignTop)

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
                      "max_travel_v": "max_travel",
                      # the Z step calibration's Z distances too (Lukas
                      # 2026-09-29: they showed the raw "_v" names)
                      "zcal_step_v": "zcal_step",
                      "zcal_start_offset_v": "zcal_start_offset",
                      "zcal_max_travel_v": "zcal_max_travel"}

    def _label_z_fields(self, unit: str):
        labels = self._form_labels.get("autofocus", {})
        # values that come from the current objective (objectives.ini) carry
        # its name, e.g. "coarse_step (um) [63x]" (Lukas 2026-09-29: the
        # autofocus distances depend on the lens)
        tags = getattr(self, "_af_obj_tags", (set(), ""))
        for name, text in self._Z_UNIT_FIELDS.items():
            lab = labels.get(name)
            if lab is None:
                continue
            tagged = name in tags[0]
            _set_label(lab, f"{text} ({unit})" + (f" [{tags[1]}]" if tagged else ""),
                       _LABEL_W_AF)
            lab.setToolTip(f"from objective {tags[1]} (objectives.ini); edit + Apply to "
                           f"change it for this session or store it for the objective"
                           if tagged else "")

    def _refresh_objective_af(self, s) -> None:
        """Follow which autofocus values come from the objective. When that
        changes (another objective selected, a value stored or edited), the
        form's numbers and tags are re-read -- the brain changed the values."""
        keys = {k for k in str(getattr(s, "af_objective_keys", "")).split(",") if k}
        sig = (frozenset(keys), str(getattr(s, "objective_name", "")))
        if sig == getattr(self, "_af_obj_sig", None):
            return
        first = getattr(self, "_af_obj_sig", None) is None
        self._af_obj_sig = sig
        self._af_obj_tags = (set(keys), sig[1])
        if not first:
            self.ctrl_get_config_into_cfg()
            self._sync_form("autofocus")
        self._label_z_fields(self._z_unit)

    def _offer_store_for_objective(self, edited: dict, before: dict) -> None:
        """After an Apply that changed per-objective autofocus distances: store
        them for the current objective (objectives.ini) or keep them for this
        session only. Asked, never assumed: objectives.ini is lab data."""
        from ..objectives import AF_KEYS
        keys = [k for k in edited if k in AF_KEYS]
        obj = self.cfg.image.objective_name
        if not keys or not obj:
            return
        from PySide6.QtWidgets import QMessageBox
        names = ", ".join(f"{k} = {edited[k]:g}" for k in keys)
        ans = QMessageBox.question(
            self, "Autofocus distances",
            f"Store {names} for objective {obj} (objectives.ini)?\n\n"
            f"Yes: used whenever {obj} is selected.\nNo: this session only.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            self._log_event("info", f"{names}: this session only")
            return
        for k in keys:
            try:
                rep = self.ctrl.store_objective_af(k, edited[k], before.get(k))
                self._log_event("info", f"{k} = {edited[k]:g} stored for objective {obj} "
                                        f"({rep.get('path', '')})")
            except Exception as exc:
                self._log_event("error", f"could not store {k} for {obj}: {exc}")

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
            row_widget.setVisible(not s.limits_from_stage)
            lab = self._form_labels.get("limits", {}).get(name)
            if lab is not None:
                lab.setVisible(not s.limits_from_stage)
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

    def _datum_z(self):
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self, "Datum Z",
            "Set the Z step counter to 0 HERE -- nothing moves; use it at a focus "
            "you trust.\n\nZ positions noted before this, and kim's leash box in Z, "
            "will refer to the new 0.")
        if ans != QMessageBox.Yes:
            return
        try:
            self.ctrl.datum_z()
        except Exception as exc:
            self._log_event("warn", f"Datum Z: {exc}")

    def _settings_tab(self, groups, compact=False, columns=None,
                      extras=None, partial=(), save=False,
                      label_w: int = _LABEL_W) -> QWidget:
        """A settings form for ``groups`` [(name, dataclass)] with ONE Apply.

        ``compact`` (the scan form under the live view): label/field pairs,
        COMPACT_COLS per row, one card per group -- as before. Otherwise
        (2026-09-29, Lukas: "one long single-column form with full-width
        fields") THREE COLUMNS of compact group boxes: ``columns`` names them
        (see AF_LAYOUT); without it each group is split over the three columns
        in field order. Fields are sized to their content. ``extras`` =
        {field: widget} placed right of that field (the Auto exposure button).
        ``partial``: groups of which the tab shows only the fields its layout
        names (the Spot knobs in the AutoFocus tab) -- no "More" box for them.
        ``save``: a "Save config" button next to Apply (see _save_config).
        Apply sends only what was EDITED (see _apply_settings).
        """
        w = QWidget(); outer = QVBoxLayout(w)
        objs = {gname.lower(): obj for gname, obj in groups}
        extras = extras or {}
        if compact:
            for gname, obj in groups:
                f, l = _card(gname)
                grid = QGridLayout()
                grid.setHorizontalSpacing(10)
                for c in range(COMPACT_COLS):
                    grid.setColumnStretch(2 * c + 1, 1)
                for i, fld in enumerate(fields(obj)):
                    widget, lab = self._field_widget(gname.lower(), obj, fld.name)
                    r, c = divmod(i, COMPACT_COLS)
                    grid.addWidget(lab, r, 2 * c)
                    grid.addWidget(widget, r, 2 * c + 1)
                l.addLayout(grid)
                outer.addWidget(f)
        else:
            if columns is None:
                columns = [[] for _ in range(3)]
                for gname, obj in groups:
                    names = [fl.name for fl in fields(obj)]
                    per = max(1, -(-len(names) // 3))
                    for c in range(3):
                        part = names[c * per:(c + 1) * per]
                        if part:
                            title = gname if c == 0 else f"{gname} (cont.)"
                            columns[c].append((title, [(gname.lower(), n) for n in part]))
            placed = {(g, n) for col in columns for _t, items in col for g, n in items}
            more = [(g, fl.name) for g, obj in objs.items() for fl in fields(obj)
                    if (g, fl.name) not in placed and g not in partial]
            if more:
                columns = [list(c) for c in columns]
                columns[-1].append(("More", more))
            row = QHBoxLayout(); row.setSpacing(10)
            self._boxes = getattr(self, "_boxes", {})
            for col in columns:
                cv = QVBoxLayout(); cv.setSpacing(8)
                for title, items in col:
                    items = [(g, n) for g, n in items if g in objs and hasattr(objs[g], n)]
                    if not items:
                        continue
                    f, l = _card(title)
                    grid = QGridLayout(); grid.setHorizontalSpacing(8)
                    grid.setVerticalSpacing(4)
                    for r, (g, n) in enumerate(items):
                        widget, lab = self._field_widget(g, objs[g], n, label_w)
                        grid.addWidget(lab, r, 0)
                        if n in extras:
                            holder = QWidget(); hl = QHBoxLayout(holder)
                            hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(6)
                            hl.addWidget(widget); hl.addWidget(extras[n]); hl.addStretch(1)
                            grid.addWidget(holder, r, 1)
                        else:
                            grid.addWidget(widget, r, 1, Qt.AlignLeft)
                    grid.setColumnStretch(2, 1)
                    l.addLayout(grid)
                    cv.addWidget(f)
                    self._boxes[title] = f
                cv.addStretch(1)
                row.addLayout(cv)
            row.addStretch(1)
            outer.addLayout(row)
        if "limits" in objs:
            # Shown instead of the envelope rows when the stage owns its
            # limits (the KIM rig): 0..130 um / 0..75 V mean nothing there.
            self.lab_limits_note = QLabel(""); self.lab_limits_note.setWordWrap(True)
            self.lab_limits_note.setVisible(False)
            outer.addWidget(self.lab_limits_note)
            self._limit_rows = {k: v for k, v in self._form_widgets["limits"].items()
                                if k != "enforce"}
        b = QPushButton("Apply settings"); b.setObjectName("primary")
        b.clicked.connect(lambda _=False, gs=groups: self._apply_settings(gs))
        rowb = QHBoxLayout(); rowb.addWidget(b)
        if save:
            rowb.addWidget(self._save_config_button())
        rowb.addStretch(1)
        outer.addLayout(rowb)
        outer.addStretch(1)
        return w

    def _field_widget(self, key: str, obj, name: str, label_w: int = _LABEL_W):
        """(editing widget, its label) for one config field, registered for
        Apply / sync (group ``key``)."""
        if name == "objective_name":
            widget = QComboBox()
            widget.addItems(self._objective_names)
            cur = str(getattr(obj, name))
            idx = widget.findText(cur)
            widget.setCurrentIndex(idx if idx >= 0 else 0)
            # connect AFTER setting the index so setup doesn't fire it
            widget.currentTextChanged.connect(self._on_objective_changed)
            self._objective_combo = widget
            getter = widget.currentText
            _size_widget(name, widget)
        else:
            widget, getter = _sized_widget_for_field(name, getattr(obj, name))
        if name == "xy_unit":
            # a display choice: takes effect at once, no Apply needed
            widget.currentTextChanged.connect(self._on_xy_unit_changed)
        _guard_wheel(widget)
        lab = QLabel()
        _set_label(lab, name, label_w)
        self._getters.setdefault(key, {})[name] = getter
        self._form_widgets.setdefault(key, {})[name] = widget
        self._form_labels.setdefault(key, {})[name] = lab
        self._form_shown.setdefault(key, {})[name] = getter()
        if name == "pixel_size_x_um":
            self._pxx_widget = widget
        elif name == "pixel_size_y_um":
            self._pxy_widget = widget
        return widget, lab

    def _on_tab_changed(self, _i: int) -> None:
        page = self.tabs.currentWidget()
        if page is getattr(self, "af_page", None):
            self._sync_form("spot")
            self._sync_form("autofocus")
            self._af_rules()

    def _camera_settings_tab(self) -> QWidget:
        outer = QWidget(); ov = QVBoxLayout(outer)
        # Order (Lukas, 2026-09-14): Image first (objective / pixel size, used all
        # the time), then Camera, then the long list of live camera parameters.
        # Auto exposure (once), right next to the exposure it sets (Lukas
        # 2026-09-29): an explicit action; it changes only ExposureTime
        self.b_auto_expo = QPushButton("Auto exposure (once)")
        self.b_auto_expo.setToolTip("The camera's ExposureAuto=Once if it has it, else a "
                                    "few software steps that bring the IMAGE (the spot's "
                                    "search region left out) to auto_exposure_target of "
                                    "full scale. Changes only ExposureTime.")
        self.b_auto_expo.clicked.connect(self._auto_exposure)
        ov.addWidget(self._settings_tab([("Image", self.cfg.image),
                                         ("Camera", self.cfg.camera)],
                                        columns=CAMERA_LAYOUT,
                                        extras={"exposure_us": self.b_auto_expo},
                                        save=True))
        # the four clip edges mean nothing while clipping is off: greyed
        clip = self._form_widgets.get("image", {}).get("clip_enabled")
        if clip is not None:
            clip.toggled.connect(self._clip_rules)
            self._clip_rules()
        # the simulator's own settings only when the camera IS the simulator
        box = getattr(self, "_boxes", {}).get("Simulator")
        if box is not None:
            box.setVisible(self._camera_is_sim())
        # live camera parameters (built from the backend's feature list)
        f, l = _card("Camera parameters (live)")
        row = QHBoxLayout()
        row.addWidget(QLabel("Read live from the camera; changes apply immediately."))
        row.addStretch(1)
        b = QPushButton("Refresh"); b.clicked.connect(self._build_cam_params)
        row.addWidget(b); mark_always(b)        # only reads
        l.addLayout(row)
        self._cam_param_host = QWidget()
        self._cam_param_form = QFormLayout(self._cam_param_host)
        self._cam_param_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        l.addWidget(self._cam_param_host)
        ov.addWidget(f)
        ov.addStretch(1)
        self._build_cam_params()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(outer)
        return scroll

    def _camera_is_sim(self) -> bool:
        """Is the camera behind this GUI the simulator? (its model name says so)"""
        try:
            feats = self.ctrl.camera_features() or []
        except Exception:
            return False
        return any(f.get("name") == "DeviceModelName"
                   and str(f.get("value", "")).lower().startswith("sim") for f in feats)

    def _clip_rules(self, *_):
        on = self._form_widgets["image"]["clip_enabled"].isChecked()
        for k in ("clip_left", "clip_top", "clip_right", "clip_bottom"):
            for w in (self._form_widgets["image"].get(k), self._form_labels["image"].get(k)):
                if w is not None:
                    w.setEnabled(on)
                    w.setToolTip("" if on else "clip_enabled is off: not used")

    def _auto_exposure(self):
        try:
            res = self.ctrl.auto_exposure_once()
        except Exception as exc:
            self._log_event("error", f"auto exposure: {exc}")
            return
        self.cfg.camera.exposure_us = float(res.get("new_us", self.cfg.camera.exposure_us))
        self._sync_form("camera")
        self._build_cam_params()

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
            _size_widget(ft.get("name", ""), w)       # sized to content, not the window
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
        # Only the fields the user EDITED since the form last showed them. The
        # whole form used to be sent, and fields that change elsewhere -- the
        # exposure set in the live parameter panel, the scan point picked with
        # Index X/Y or by a scan -- were silently put back to the form's old
        # numbers by an unrelated Apply (deep cleaning 2026-09-28).
        payload = {}
        before_af = dict(self._form_shown.get("autofocus", {}))   # for "store for objective"
        for gname, _obj in groups:
            key = gname.lower()
            shown = self._form_shown.get(key, {})
            edited = {}
            for n, get in self._getters.get(key, {}).items():
                v = get()
                if n not in shown or v != shown[n]:
                    edited[n] = v
            payload[key] = edited
        if "scanning" in payload:
            self._keep_scan_size(payload["scanning"])
        try:
            self.ctrl.set_config(payload)
            for key, vals in payload.items():     # applied: now that is what is shown
                self._form_shown.setdefault(key, {}).update(vals)
            # keep our local cfg mirror in step (used to draw overlays)
            self.ctrl_get_config_into_cfg()
            self._sync_size_fields()          # pitch/points edits update size too
            if "scanning" in payload:
                self._sync_form("scanning")   # show the pitch a points change produced
            self._log_event("info", f"applied settings: {', '.join(payload)}")
        except Exception as exc:
            self._log_event("error", f"apply failed: {exc}")
            return
        if payload.get("autofocus"):
            self._offer_store_for_objective(payload["autofocus"], before_af)

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
            get = self._getters.get(group, {}).get(name)
            if get is not None:                  # what the form shows now
                self._form_shown.setdefault(group, {})[name] = get()

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

    def _refresh_af_sizes(self, s) -> None:
        """AutoFocus tab: this frame's spot sizes, the metric in use marked."""
        lab = getattr(self, "lab_af_sizes", None)
        if lab is None:
            return
        mech = self.cfg.autofocus.mechanism

        def num(v, fmt):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return "-"
            return format(v, fmt) if math.isfinite(v) else "-"

        txt = sizes_summary(s, highlight=mech)
        bits = int(getattr(s, "spot_bit_depth", 8) or 8)
        if bits > 8:
            txt += f" &nbsp;<i>({bits}-bit frame)</i>"
        else:
            # why not deeper (2026-09-29): the camera's PixelFormat, said once
            # in the log and kept here next to the sizes it limits
            why = getattr(s, "spot_bit_note", "") or ""
            if why:
                txt += f"<br><i>8-bit frame: {html.escape(why)}</i>"
        hint = getattr(s, "af_hint", "")
        if hint:
            # spot_area on an unsaturated spot: said here, not silently "fixed"
            txt += f"<br><span style='color:{T.COLORS['danger']}'>{hint}</span>"
        lab.setText(txt)

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
        label = AF_METRIC_LABELS.get(self.cfg.autofocus.mechanism, "focus")
        self.af_plot._ylabel = "metric"        # (the Z calibration view relabels it)
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
        if self._control_bar is not None:
            self._control_bar.refresh()
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
        self._refresh_zoom(s)               # every tab: a run must not be missed

        _set_led(self.led_match, s.match_found, T.OK)
        if s.backups_n:
            drv = "main" if s.pattern_driver == 0 else f"backup {s.pattern_driver}"
            self.lab_patterns.setText(f"{s.backups_n} backup(s) - driving: {drv}")
        else:
            self.lab_patterns.setText("no backups")
        _set_led(self.led_stable, s.stable, T.OK)
        # The stabiliser can be switched by something else -- placing the laser
        # switches it off, a scan or a console may switch it -- so the box
        # follows the brain (blockSignals: the change must not be sent back).
        if self.chk_stab.isChecked() != bool(s.stabilize_on):
            self.chk_stab.blockSignals(True)
            self.chk_stab.setChecked(bool(s.stabilize_on))
            self.chk_stab.blockSignals(False)
        self._refresh_laser(s)
        _set_led(self.led_af, not s.af_running and s.af_error == "OK", T.OK)
        zcal_on = bool(getattr(s, "zcal_running", False))
        # af_error now carries the failure's MESSAGE (rig 2026-09-29), which can
        # be long: the small label shows its start, the tooltip all of it
        af_txt = "run" if s.af_running else ("Z cal" if zcal_on else s.af_error)
        self.lab_af.setText(af_txt if len(af_txt) <= 40 else af_txt[:37] + "...")
        self.lab_af.setToolTip("" if af_txt == s.af_error == "OK" else af_txt)
        # Z follows the Z device: volts on the piezo rig, um on the KIM rig,
        # whose range (kim's leash) can change while we run.
        if s.z_unit != self._z_unit:
            self._z_unit = s.z_unit
            self.z_spin.setSuffix(f" {s.z_unit}")
            self.z_step.setSuffix(f" {s.z_unit}")
            self.af_plot._xlabel = f"Z ({s.z_unit})"
            self._label_z_fields(s.z_unit)
        self._refresh_objective_af(s)
        if (s.z_min, s.z_max) != (self.z_spin.minimum(), self.z_spin.maximum()):
            self.z_spin.setRange(s.z_min, s.z_max)
        self.lab_best.setText(f"{s.best_focus_v:.2f} {s.z_unit}")
        self._refresh_af_sizes(s)
        on = bool(getattr(s, "af_exposure_active", False))
        self.lab_af_expo.setVisible(on)
        # the short AF exposure makes the picture almost black: stretch the
        # DISPLAY while it is active (the measured data is unchanged)
        self.view.set_stretch(on)
        if on:
            self.lab_af_expo.setText(f"<b>autofocus exposure ON</b> "
                                     f"({self.cfg.autofocus.exposure_us:g} us) -- the working "
                                     f"exposure comes back when the run ends; the picture's "
                                     f"contrast is stretched meanwhile (display only)")
        self._refresh_zcal(s)
        self._refresh_xy(s)
        self._sync_stage(s)                 # after _refresh_xy: it may re-enable Datum
        self._sync_fault(s)
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

    # -- Save config (2026-09-29) ------------------------------------------ #
    # Before this the only way to keep settings over a restart was the button
    # in the Spot tab. The same save is now next to Apply in the AutoFocus and
    # Camera settings tabs. It writes the WHOLE config the camera is USING
    # (local brain or the service, via ctrl.save_config) -- so Apply first:
    # a form field not applied yet is not in the brain, so not in the file.
    def _save_config_button(self) -> QPushButton:
        b = QPushButton("Save config")
        b.setToolTip(SAVE_CONFIG_TIP)
        b.clicked.connect(self._save_config)
        self._save_buttons = getattr(self, "_save_buttons", []) + [b]
        return b

    def _save_config(self) -> None:
        try:
            path = self.ctrl.save_config()
            self._log_event("info", f"camera settings saved to {path} "
                                    f"(loaded at service start)")
        except Exception as exc:
            self._log_event("error", f"save failed: {exc}")

    # -- zoom to the spot region (2026-09-29) ------------------------------ #
    def _frame_size(self) -> tuple:
        return (self.view._frame_w, self.view._frame_h)

    def _spot_zoom(self, s):
        return spot_zoom_rect(s, self.cfg.spot, *self._frame_size())

    def _user_view(self, s):
        """The view the user chose: the spot region (their toggle, following a
        new calibration) or whatever zoom was on before the run."""
        if self._zoom_spot_on:
            return self._spot_zoom(s)
        return self._zoom_saved

    def _toggle_zoom(self) -> None:
        s = self._last_status()
        if self._af_zoom_active and not self._af_zoom_dismissed:
            self._dismiss_af_zoom()            # = "Whole frame" during the AF zoom
            return
        self._zoom_spot_on = not self._zoom_spot_on
        # during a (dismissed) run the user's choice is what comes back at the end
        self._zoom_saved = self._spot_zoom(s) if self._zoom_spot_on else None
        self.view.set_zoom(self._zoom_saved)
        self._zoom_button_text()

    def _dismiss_af_zoom(self) -> None:
        """Double-click on the autofocus zoom (or the button): the whole frame
        for the REST of this run; the next run zooms again."""
        if not self._af_zoom_active:
            return
        self._af_zoom_dismissed = True
        self._zoom_spot_on = False
        self.view.set_zoom(None)
        self.view.set_zoom_note("")
        self._zoom_button_text()

    def _zoom_button_text(self) -> None:
        zoomed = self.view.zoom() is not None
        self.b_zoom.setText(ZOOM_OUT_TEXT if zoomed else ZOOM_IN_TEXT)

    def _last_status(self):
        s = getattr(self, "_status_seen", None)
        return s if s is not None else self.ctrl.status()

    def _refresh_zoom(self, s) -> None:
        """Zoom the main view onto the spot region while an autofocus runs.

        A RUN = an autofocus (af_running; also the recovery autofocus after a
        lost pattern, which is an ordinary numbered run) or a Z step
        calibration (zcal_running). A run starts when one of them rises, or
        when af_id / zcal_id changes while running (a queued run that follows
        straight on). At the start the view the user had is remembered; at the
        end it comes back. autofocus.zoom_on_af off = nothing happens.
        """
        self._status_seen = s
        running = bool(getattr(s, "af_running", False) or getattr(s, "zcal_running", False))
        key = (getattr(s, "af_id", 0), getattr(s, "zcal_id", 0))
        started = running and (not self._zoom_run_was or key != self._zoom_run_key)
        ended = self._zoom_run_was and not running
        self._zoom_run_was, self._zoom_run_key = running, key
        if started:
            self._af_zoom_dismissed = False     # a new run zooms again
            if bool(getattr(self.cfg.autofocus, "zoom_on_af", True)) and \
                    not self._af_zoom_active:
                self._zoom_saved = self.view.zoom()
                self._af_zoom_active = True
        if ended and self._af_zoom_active:
            self._af_zoom_active = False
            self._af_zoom_dismissed = False
            self.view.set_zoom_note("")
            self.view.set_zoom(self._user_view(s))
        elif self._af_zoom_active and not self._af_zoom_dismissed:
            # re-evaluated every refresh: a recalibrated spot moves the zoom
            self.view.set_zoom(self._spot_zoom(s))
            self.view.set_zoom_note(AF_ZOOM_NOTE)
        elif not self._af_zoom_active and self._zoom_spot_on:
            self.view.set_zoom(self._spot_zoom(s))   # follow a new calibration
        self._zoom_button_text()

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
