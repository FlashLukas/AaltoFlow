"""Switchable layouts for the Camera tab (Lukas, 2026-10-13).

The Camera tab has one set of controls -- built once by
``MainWindow._build_camera_controls`` -- and six ways to arrange them:

* **classic**  -- the arrangement the tab had before (image left, four cards
  right, Control XY stage / Define scanning / Check alignment accuracy below).
* **cockpit**  -- image in the centre; a vertical Focus (Z) column left; right
  the Lock / Laser tabs and the XY block; imaging + scan pages below.
* **workflow** -- a stepper 1 Focus, 2 Spot, 3 Pattern, 4 Lock, 5 Place / scan
  with a light per step; only the chosen step's controls are shown; a strip
  with XY jog, Z +/- and Kill AF is always there.
* **hud**      -- the image fills the tab; small translucent panels ON the image
  (status, zoom, Z rocker, jog pad, the three switches); the rest in a slim
  tab bar below.
* **split**    -- by axis: everything Z left, the image in the middle,
  everything XY right.
* **image_first** -- (2026-10-14, "B") the image fills the tab; a toolbar
  over it (the switches with their lights, Find focus, Kill AF, Snapshot,
  zoom, an Overlays drop-down); a right drawer with the tabs Focus, Lock,
  Stage, Laser, Scan that folds to a thin strip; a status line under it.

HOW A SWITCH WORKS. A layout never builds a control. It builds its own
CONTAINERS (cards, rows, tabs, splitters, the overlay panels) and RE-PARENTS
the window's existing widgets into them (Qt's setParent / addWidget). Switching
to another layout first moves every control into a hidden "parking" widget,
then throws the old containers away and builds the new ones. So:

* the same objects in every layout: signals, the attribute names the code and
  the tests use, tool tips, enabled states all travel with the widget;
* the viewer guard (control_bar.py) works unchanged: it reads a property ON the
  widget and walks up to the window, whatever the containers are;
* nothing is copied, so there is no second piece of logic to keep in step.

A container label ("step", "Index X") is part of the layout and is rebuilt
with it; only CONTROLS are listed in ``PIECES``.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QBoxLayout, QLayout, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QTabBar, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme as T
from .control_bar import mark_always

#: key -> the name shown in the Layout box (order = the box's order)
LAYOUT_TITLES = {
    "classic": "Classic",
    "cockpit": "Cockpit",
    "workflow": "Workflow steps",
    "hud": "Heads-up",
    "split": "Split by axis",
    "image_first": "Image first",
}

#: Every control of the Camera tab a layout places (MainWindow attributes).
#: A layout that leaves one out must name it in its OPTIONAL set -- the tests
#: check that every other one is reachable in every layout.
PIECES = (
    "view", "box_zoom", "status_strip", "fault_bar", "stage_bar_top",
    # focus (Z)
    "z_spin", "b_setz", "lab_z", "z_step", "b_z_up", "b_z_dn", "b_datum_z", "b_af",
    "b_kill", "b_af_here", "b_af_at", "lab_afpos", "led_af", "lab_af", "lab_best",
    "chk_cont",
    # pattern tracking
    "chk_track", "led_match", "chk_roi", "chk_backup", "box_search", "lab_patterns",
    "b_clr_backups", "b_load_pat", "b_save_pat",
    # stabiliser
    "chk_stab", "led_stable", "box_index", "lab_drive", "box_stab_tuning", "lab_stab",
    # imaging
    "b_snap", "chk_click", "chk_thr", "chk_spot_info", "chk_pat_info", "chk_points",
    # XY stage + the bottom pages
    "card_position", "card_jog", "card_goto", "card_laser", "page_scanning",
    "page_accuracy",
)

#: per layout: pieces it does not show (parked, hidden)
OPTIONAL = {
    "classic": {"status_strip"},        # the layout as it was: no status line
}


# --------------------------------------------------------------------------- #
# containers
# --------------------------------------------------------------------------- #
def _fill(lay, items) -> None:
    """Add ``items`` to a box layout: a widget, a label text (str), None =
    stretch, or (widget, stretch)."""
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, str):
            lay.addWidget(QLabel(it))
        elif isinstance(it, tuple):
            lay.addWidget(it[0], it[1])
        else:
            lay.addWidget(it)


def block(*items) -> QWidget:
    """A row of controls that always travel together (ONE piece for the
    layouts, built once by the window)."""
    w = QWidget(); w.setObjectName("block")
    lay = QHBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
    _fill(lay, items)
    return w


def row(*items, spacing: int = 6) -> QWidget:
    """A layout's own horizontal row (rebuilt with the layout)."""
    w = QWidget(); w.setObjectName("row")
    lay = QHBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(spacing)
    _fill(lay, items)
    return w


def col(*items, spacing: int = 6, margins: int = 0) -> QWidget:
    w = QWidget(); w.setObjectName("column")
    lay = QVBoxLayout(w); lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    _fill(lay, items)
    return w


def card(title: str, *items) -> QFrame:
    """The suite's card (QFrame#card + a small title), items stacked."""
    f = QFrame(); f.setObjectName("card")
    lay = QVBoxLayout(f); lay.setContentsMargins(10, 8, 10, 10); lay.setSpacing(6)
    lab = QLabel(title.upper()); lab.setObjectName("cardTitle")
    lay.addWidget(lab)
    _fill(lay, items)
    return f


def tabs(*pages) -> QTabWidget:
    t = QTabWidget()
    for title, page in pages:
        t.addTab(page, title)
    return t


def scroll(widget: QWidget) -> QScrollArea:
    """A page that scrolls instead of being cut off on a small screen
    (1366 x 768), and imposes no minimum size on its container."""
    a = QScrollArea(); a.setWidgetResizable(True); a.setFrameShape(QFrame.NoFrame)
    a.setWidget(widget)
    return a


class SideColumn(QScrollArea):
    """A column beside the image: exactly as wide as its content wants (plus
    the scroll bar), and scrolling VERTICALLY when the screen is too short --
    a fixed pixel width would either cut a card off or waste space, because
    the cards' widths depend on the font and the theme."""

    def __init__(self, widget: QWidget):
        super().__init__()
        self.setWidgetResizable(True); self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidget(widget)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def _hint(self) -> QSize:
        # (QScrollArea's own hints call each other; do not chain to them)
        w = self.widget()
        width = 0 if w is None else w.sizeHint().width()
        return QSize(width + self.verticalScrollBar().sizeHint().width() + 4, 120)

    def sizeHint(self):                                 # noqa: N802 (Qt name)
        return self._hint()

    def minimumSizeHint(self):                          # noqa: N802 (Qt name)
        return self._hint()


def _top(widget: QWidget) -> QWidget:
    """Pin a page's content to the top (a tab page otherwise centres it)."""
    return col(widget, None)


def _wrap(lab: QLabel) -> QLabel:
    lab.setWordWrap(True)
    return lab


# --------------------------------------------------------------------------- #
# the status line (Cockpit / Workflow / Heads-up / Split)
# --------------------------------------------------------------------------- #
def status_parts(s, af_text: str | None = None) -> list:
    """[(colour key or None, text)] for the status line: tracking + match,
    stable, autofocus, Z, XY. Pure, so it can be tested with a fake status."""
    g = lambda k, d=None: getattr(s, k, d)      # noqa: E731 (a short reader)
    parts = []
    if not g("tracking_on", False):
        parts.append(("muted", "track off"))
    elif g("match_found", False):
        parts.append(("ok", f"match {float(g('match_score', 0.0)):.2f}"))
    else:
        parts.append(("danger", "no match"))
    if not g("stabilize_on", False):
        parts.append(("muted", "stabiliser off"))
    elif g("stable", False):
        parts.append(("ok", "stable"))
    else:
        parts.append(("accent", f"stabilising {float(g('distance_um', 0.0)):.2f} um"))
    af = af_text if af_text is not None else str(g("af_error", "OK"))
    if g("af_running", False):
        parts.append(("accent", "AF running"))
    elif af == "OK":
        parts.append(("ok" if int(g("af_id", 0) or 0) > 0 else "muted",
                      "AF OK" if int(g("af_id", 0) or 0) > 0 else "AF not run"))
    else:
        parts.append(("danger", "AF " + (af if len(af) <= 28 else af[:25] + "...")))
    parts.append((None, f"Z {float(g('z_voltage', 0.0)):.2f} {g('z_unit', 'V')}"))
    if g("xy_step_unit", "um") == "steps":
        parts.append((None, f"XY {int(g('stage_steps_x', 0)):+d}, "
                            f"{int(g('stage_steps_y', 0)):+d} steps"))
    else:
        parts.append((None, f"XY {float(g('stage_x', 0.0)):.2f}, "
                            f"{float(g('stage_y', 0.0)):.2f} um"))
    return parts


class StatusStrip(QLabel):
    """One line (or, on the image, one column) of the working state.

    Display only. The dots use the theme's colours at paint time of the text
    (set_theme runs before any widget is built, gotcha #6)."""

    def __init__(self):
        super().__init__("")
        self.setObjectName("statusStrip")
        self.setTextFormat(Qt.RichText)
        self._multiline = False
        self._parts: list = []

    def set_multiline(self, on: bool) -> None:
        self._multiline = bool(on)
        self._render()

    def update_status(self, s, af_text: str | None = None) -> None:
        parts = status_parts(s, af_text)
        if parts != self._parts:
            self._parts = parts
            self._render()

    def _render(self) -> None:
        bits = []
        for key, text in self._parts:
            dot = (f'<span style="color:{T.COLORS[key]}">&#9679;</span>&nbsp;' if key else "")
            # no break INSIDE a part ("stabilising 0.42 um" stays one piece):
            # with word wrap on (Image first, a narrow window) the line breaks
            # only between parts
            bits.append(dot + html.escape(text).replace(" ", "&nbsp;"))
        sep = "<br>" if self._multiline else "&nbsp;&nbsp; "
        self.setText(sep.join(bits))

    def one_line_width(self) -> int:
        """Pixels the parts need on ONE line (dots and gaps included)."""
        fm = self.fontMetrics()
        text = "   ".join(("● " if k else "") + t for k, t in self._parts)
        return fm.horizontalAdvance(text) + 8


class StatusLine(QWidget):
    """Image first's line under the image: the status, then the newest log
    line -- side by side when the width allows, else the log line under it
    (a fixed single line needed ~950 px and widened a 1366 px window)."""

    def __init__(self, strip: StatusStrip, last: QLabel):
        super().__init__()
        self.setObjectName("row")
        self.strip, self.last = strip, last
        self.sep = _vline()
        self.lay = QBoxLayout(QBoxLayout.LeftToRight, self)
        self.lay.setContentsMargins(0, 0, 0, 0); self.lay.setSpacing(10)
        self.lay.addWidget(strip); self.lay.addWidget(self.sep); self.lay.addWidget(last, 1)
        strip.setWordWrap(True)
        strip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def _arrange(self) -> None:
        wide = self.width() >= self.strip.one_line_width() + 260
        d = QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom
        if self.lay.direction() != d:
            self.lay.setDirection(d)
            self.sep.setVisible(wide)
        if wide:                       # one line: exactly as wide as its text
            self.strip.setFixedWidth(self.strip.one_line_width())
        else:                          # wraps between parts as needed
            self.strip.setMinimumWidth(0); self.strip.setMaximumWidth(16777215)

    def resizeEvent(self, ev):                          # noqa: N802 (Qt name)
        self._arrange()
        super().resizeEvent(ev)

    def minimumSizeHint(self):                          # noqa: N802
        return QSize(120, super().minimumSizeHint().height())


# --------------------------------------------------------------------------- #
# Workflow: the five steps and their lights
# --------------------------------------------------------------------------- #
STEPS = (("focus", "1  Focus"), ("spot", "2  Spot"), ("pattern", "3  Pattern"),
         ("lock", "4  Lock"), ("place", "5  Place / scan"))
#: light level -> theme colour key (None = the LED's "off" look)
LEVEL_COLOUR = {"ok": "ok", "busy": "accent", "bad": "danger", "off": None}


def workflow_states(s) -> dict:
    """step key -> (level, text); level in ok / busy / bad / off. Pure: the
    lights follow the service's status and nothing else."""
    g = lambda k, d=None: getattr(s, k, d)      # noqa: E731
    out = {}
    if g("af_running", False):
        out["focus"] = ("busy", "autofocus running")
    elif int(g("af_id", 0) or 0) <= 0:
        out["focus"] = ("off", "no autofocus yet")
    elif g("af_error", "OK") == "OK":
        out["focus"] = ("ok", f"focused (best {float(g('best_focus_v', 0.0)):.2f} "
                              f"{g('z_unit', 'V')})")
    else:
        out["focus"] = ("bad", "autofocus failed")
    out["spot"] = (("ok", "calibrated") if g("spot_calibrated", False)
                   else ("off", "not calibrated"))
    nb = int(g("backups_n", 0) or 0)
    extra = f" (+{nb} backup{'s' if nb != 1 else ''})" if nb else ""
    if not g("tracking_on", False):
        out["pattern"] = ("off", "tracking off")
    elif g("match_found", False):
        out["pattern"] = ("ok", f"matched {float(g('match_score', 0.0)):.2f}{extra}")
    else:
        out["pattern"] = ("bad", "tracking on, no match" + extra)
    if not g("stabilize_on", False):
        out["lock"] = ("off", "stabiliser off")
    elif g("stable", False):
        out["lock"] = ("ok", "stable")
    else:
        out["lock"] = ("busy", f"settling, {float(g('distance_um', 0.0)):.2f} um")
    if g("laser_goto", False):
        out["place"] = ("busy", "placing the laser")
    elif g("laser_settled", False):
        out["place"] = ("ok", "laser placed")
    elif g("stabilize_on", False) and g("point_settled", False):
        out["place"] = ("ok", "scan point settled")
    elif g("stabilize_on", False):
        out["place"] = ("busy", "going to the scan point")
    else:
        out["place"] = ("off", "nothing placed")
    return out


def _paint_led(lab: QLabel, level: str) -> None:
    key = LEVEL_COLOUR.get(level)
    c = T.COLORS[key] if key else T.COLORS["panel_hi"]
    lab.setStyleSheet(f"background:{c};border-radius:7px;")
    lab.setProperty("level", level)


class Stepper(QWidget):
    """The five steps as buttons with a light and a one-line state each.
    Clicking a step shows that step's page of ``stack``."""

    def __init__(self, stack: QStackedWidget):
        super().__init__()
        self.setObjectName("column")
        self.stack = stack
        self.buttons, self.leds, self.notes = {}, {}, {}
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        self.group = QButtonGroup(self); self.group.setExclusive(True)
        for i, (key, title) in enumerate(STEPS):
            led = QLabel(); led.setFixedSize(14, 14); _paint_led(led, "off")
            b = QPushButton(title); b.setCheckable(True)
            b.setStyleSheet("text-align: left; padding: 6px 10px;")
            b.setToolTip("show only this step's controls")
            mark_always(b)               # navigation only: fine for a viewer
            note = QLabel("-"); note.setObjectName("muted")
            self.group.addButton(b, i)
            lay.addWidget(row(led, (b, 1), (note, 2)))
            self.buttons[key], self.leds[key], self.notes[key] = b, led, note
        self.group.idClicked.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._follow)
        self._follow(self.stack.currentIndex())

    def _follow(self, i: int) -> None:
        b = self.group.button(i)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def update_status(self, s) -> None:
        for key, (level, text) in workflow_states(s).items():
            if self.leds[key].property("level") != level:
                _paint_led(self.leds[key], level)
            if self.notes[key].text() != text:
                self.notes[key].setText(text)


# --------------------------------------------------------------------------- #
# Heads-up: translucent panels on the image
# --------------------------------------------------------------------------- #
def _rgba(key: str, alpha: int) -> str:
    c = QColor(T.COLORS[key])
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


class HudPanel(QFrame):
    """A small panel drawn ON the camera view (a child of the view).

    Translucent theme colours, so it reads on a dark or a bright frame and in
    either theme. It takes the clicks inside its own rectangle only -- the
    rest of the image still gets click-to-go, ROI and scan-area drags.
    ``passive``: a read-only panel lets even its own clicks fall through.
    """

    def __init__(self, view: QWidget, *items, vertical: bool = True,
                 passive: bool = False):
        super().__init__(view)
        self.setObjectName("hud")
        lay = (QVBoxLayout if vertical else QHBoxLayout)(self)
        lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(4)
        _fill(lay, items)
        self.setStyleSheet(
            f"QFrame#hud {{ background: {_rgba('panel', 215)};"
            f" border: 1px solid {_rgba('border', 230)}; border-radius: 8px; }}"
            "QFrame#hud QLabel, QFrame#hud QCheckBox, QFrame#hud QWidget#row,"
            " QFrame#hud QWidget#block, QFrame#hud QWidget#column"
            " { background: transparent; }"
            "QFrame#hud QFrame#card { background: transparent; border: none; }")
        if passive:
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)


class _HudPlacer(QObject):
    """Keeps the panels in their corners when the view is resized."""

    def __init__(self, view: QWidget, place):
        super().__init__(view)
        self._place = place
        view.installEventFilter(self)

    def eventFilter(self, obj, ev):                  # noqa: N802 (Qt name)
        if ev.type() in (QEvent.Resize, QEvent.Show):
            self._place()
        return False


# --------------------------------------------------------------------------- #
# the cards several layouts share (containers only; the controls are the
# window's)
# --------------------------------------------------------------------------- #
def _pattern_card(w, with_switch: bool = True) -> QFrame:
    first = (row(w.chk_track, None, "Match", w.led_match) if with_switch
             else row("Match", w.led_match, None))
    return card("Pattern tracking", first,
                row(w.chk_roi, w.chk_backup),
                w.box_search,
                row((w.lab_patterns, 1), w.b_clr_backups),
                row(w.b_load_pat, w.b_save_pat))


def _stab_card(w, with_switch: bool = True) -> QFrame:
    first = (row(w.chk_stab, None, "Stable", w.led_stable) if with_switch
             else row("Stable", w.led_stable, None))
    return card("Stabiliser", first, w.box_index, w.lab_drive, w.box_stab_tuning,
                w.lab_stab)


def _imaging_card(w, with_click: bool = True) -> QFrame:
    return card("Imaging",
                row(w.b_snap, w.chk_click) if with_click else row(w.b_snap, None),
                w.chk_thr,
                row(w.chk_spot_info, w.chk_pat_info, None),
                w.chk_points)


def _af_state_row(w) -> QWidget:
    return row("AF", w.led_af, w.lab_af, None, "Best", w.lab_best)


def _bottom_pages(w) -> QTabWidget:
    # each page scrolls on its own: the scan form is wide, and must not make
    # the whole tab scroll sideways on a small screen
    return tabs(("Define scanning", scroll(w.page_scanning)),
                ("Check alignment accuracy", w.page_accuracy))


def _vsplit(top: QWidget, bottom: QWidget, bottom_px: int = 240) -> QSplitter:
    """Image area above, the scan pages below; the user can drag the line.
    The image gets everything but ``bottom_px`` at first."""
    sp = QSplitter(Qt.Vertical)
    sp.addWidget(top); sp.addWidget(bottom)
    sp.setChildrenCollapsible(False)
    sp.setStretchFactor(0, 1); sp.setStretchFactor(1, 0)
    sp.setSizes([10000, bottom_px])     # scaled to the real height, ratio kept
    return sp


def _hbox(*items, spacing: int = 6) -> QWidget:
    """Columns side by side, each pinned to the top."""
    w = QWidget(); w.setObjectName("row")
    lay = QHBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(spacing)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, tuple):
            lay.addWidget(it[0], it[1])
        else:
            lay.addWidget(it, 0, Qt.AlignTop)
    return w


def _pages_follow_current(stack: QStackedWidget) -> None:
    """A stack is as large as its LARGEST page; here only the shown page
    counts, so a short step does not inherit the size of a long one."""
    def follow(i):
        for k in range(stack.count()):
            pol = QSizePolicy.Preferred if k == i else QSizePolicy.Ignored
            stack.widget(k).setSizePolicy(pol, pol)
        stack.updateGeometry()
    stack.currentChanged.connect(follow)
    follow(stack.currentIndex())


class Built:
    """What a layout builder hands back: its root, extra containers that do
    not live under the root (the panels on the image), and two hooks."""

    def __init__(self, root, extras=(), update=None, teardown=None):
        self.root = root
        self.extras = list(extras)
        self.update = update
        self.teardown = teardown


# --------------------------------------------------------------------------- #
# the layouts
# --------------------------------------------------------------------------- #
def build_classic(w) -> Built:
    """The Camera tab as it was before layouts existed (the default)."""
    top = QWidget(); top.setObjectName("column")
    lay = QHBoxLayout(top)
    # The controls sit in TWO columns beside the image, not one. In one
    # column they were ~800 px tall and pushed the bottom tabs ("Define
    # scanning", ...) below the screen (Lukas, 2026-09-25).
    lay.addWidget(col((w.view, 1), w.box_zoom, spacing=4), 4)
    focus = card("Focus (Z)",
                 row("Z target", w.z_spin, w.b_setz, w.lab_z),
                 row("step", w.z_step, w.b_z_up, w.b_z_dn, w.b_datum_z),
                 row(w.b_af, w.b_kill),
                 row(w.b_af_here, w.b_af_at),
                 w.lab_afpos,
                 _af_state_row(w),
                 w.chk_cont)
    right = QWidget(); right.setObjectName("column")
    grid = QGridLayout(right); grid.setContentsMargins(0, 0, 0, 0)
    # The FAULT banner first, above everything: a lost pattern stops the
    # stabiliser and pauses a scan, so it must be the first thing seen.
    grid.addWidget(w.fault_bar, 0, 0, 1, 2)
    grid.addWidget(w.stage_bar_top, 1, 0, 1, 2)
    # column 1: what you do to the SAMPLE's image (focus, pattern);
    # column 2: what runs on it (stabiliser) and what is drawn (imaging).
    grid.addWidget(focus, 2, 0)
    grid.addWidget(_pattern_card(w), 3, 0)
    grid.addWidget(_stab_card(w), 2, 1)
    grid.addWidget(_imaging_card(w), 3, 1)
    grid.setRowStretch(4, 1)
    grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
    lay.addWidget(right, 5)
    # Control XY stage: three compact cards + the laser card side by side,
    # each pinned top-left; the spare width stays on the right.
    xy = QWidget(); xy.setObjectName("column")
    xl = QHBoxLayout(xy); xl.setContentsMargins(4, 4, 4, 4); xl.setSpacing(10)
    for c in (w.card_position, w.card_jog, w.card_goto, w.card_laser):
        xl.addWidget(c, 0, Qt.AlignTop)
    xl.addStretch(1)
    sub = tabs(("Control XY stage", xy), ("Define scanning", w.page_scanning),
               ("Check alignment accuracy", w.page_accuracy))
    outer = QWidget(); outer.setObjectName("column")
    ov = QVBoxLayout(outer)
    ov.addWidget(top, 3); ov.addWidget(sub, 2)
    return Built(outer)


def _image_column(w) -> QWidget:
    """The image with the fault banner above it and the zoom buttons below;
    the status line and the stage line on one row over the image."""
    return col(w.fault_bar, row((w.status_strip, 1), w.stage_bar_top),
               (w.view, 1), w.box_zoom, spacing=4)


def _three_columns(left: QWidget, centre: QWidget, right: QWidget) -> QWidget:
    top = QWidget(); top.setObjectName("column")
    tl = QHBoxLayout(top); tl.setContentsMargins(0, 0, 0, 0)
    tl.addWidget(left); tl.addWidget(centre, 1); tl.addWidget(right)
    return top


def build_cockpit(w) -> Built:
    # LEFT: the Z ladder top to bottom (Z+ / where Z is / Z-), the step and
    # the target, the datum, autofocus and the AF position
    w.lab_z.setAlignment(Qt.AlignCenter)
    focus = card("Focus (Z)",
                 w.b_z_up, w.lab_z, w.b_z_dn,
                 row("step", (w.z_step, 1)),
                 row("target", (w.z_spin, 1), w.b_setz),
                 w.b_datum_z,
                 row(w.b_af, w.b_kill),
                 _af_state_row(w),
                 w.b_af_here, w.b_af_at, w.lab_afpos,
                 w.chk_cont)
    left = SideColumn(col(focus, None))
    # RIGHT: Lock = tracking + stabiliser on ONE page (tracking depends on
    # nothing, stabilising needs tracking: in that order); Laser; then XY,
    # the jog pad first (the XY control used most)
    side = tabs(("Lock", _top(col(_pattern_card(w), _stab_card(w)))),
                ("Laser", _top(w.card_laser)))
    right = SideColumn(col(side, w.card_jog, w.card_position, w.card_goto, None))
    top = _three_columns(left, _image_column(w), right)
    return Built(_vsplit(top, _bottom(w)))


def _bottom(w) -> QWidget:
    """Under the image: Imaging (scrolls on a short screen) | the scan pages."""
    return _hbox((SideColumn(col(_imaging_card(w), None)), 0), (_bottom_pages(w), 1),
                 spacing=8)


def build_workflow(w, manager) -> Built:
    stack = QStackedWidget()
    spot_state = _wrap(QLabel("-"))
    b_spot_tab = QPushButton("Open the Spot tab")
    b_spot_tab.setToolTip("Thresholding the laser spot and calibrating its position "
                          "are done in the Spot tab (a grabbed frame, not the live one).")
    mark_always(b_spot_tab)
    b_spot_tab.clicked.connect(lambda: w.tabs.setCurrentWidget(w.spot_page))
    pages = [
        card("1  Focus",
             row("Z target", w.z_spin, w.b_setz, w.lab_z, None),
             row(w.b_af, w.b_datum_z, None),
             _af_state_row(w),
             row(w.b_af_here, w.b_af_at, None),
             w.lab_afpos,
             w.chk_cont,
             _wrap(QLabel("Z +/- (with its step) and Kill AF are in the strip on the "
                          "right, always there."))),
        card("2  Spot",
             _wrap(QLabel("The laser spot is found and CALIBRATED in the Spot tab, on a "
                          "grabbed frame: threshold it there, then Calibrate. Click to go, "
                          "the stabiliser and the laser placement all measure from that "
                          "calibrated position.")),
             spot_state, row(b_spot_tab, None), w.chk_thr),
        _pattern_card(w),
        _stab_card(w),
        tabs(("Laser", _top(w.card_laser)),
             ("Stage", _top(col(w.card_position, w.card_goto))),
             ("Define scanning", scroll(w.page_scanning)),
             ("Alignment", w.page_accuracy)),
    ]
    for p in pages:
        stack.addWidget(p if isinstance(p, QTabWidget) else _top(p))
    _pages_follow_current(stack)
    stepper = Stepper(stack)
    left = QWidget(); left.setObjectName("column")
    ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 4, 0)
    ll.addWidget(stepper); ll.addWidget(stack, 1)
    left_scroll = scroll(left)
    left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    left_scroll.setMinimumWidth(430)
    imaging = row(w.b_snap, w.chk_click, w.chk_spot_info, w.chk_pat_info, w.chk_points, None)
    right = col(w.fault_bar, row((w.status_strip, 1), w.stage_bar_top),
                (w.view, 1), w.box_zoom, imaging, spacing=4)
    split = QSplitter(Qt.Horizontal)
    split.addWidget(left_scroll); split.addWidget(right)
    split.setChildrenCollapsible(False)
    split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
    split.setSizes([480, 10000])
    # the always-visible strip: XY jog, Z +/-, Kill AF (the log is the
    # window's own, under every tab). A column at the right edge: under the
    # image it made the tab taller than a 1366 x 768 screen.
    z = card("Focus", row("step", (w.z_step, 1)), row(w.b_z_up, w.b_z_dn), w.b_kill)
    strip = SideColumn(col(w.card_jog, z, None))
    root = _hbox((split, 1), (strip, 0), spacing=6)

    def update(s):
        stepper.update_status(s)
        level, text = workflow_states(s)["spot"]
        spot_state.setText(f"spot: {text}" + (
            f" at ({float(getattr(s, 'spot_x', 0.0)):.1f}, "
            f"{float(getattr(s, 'spot_y', 0.0)):.1f}) px" if level == "ok" else ""))
    manager.stepper = stepper
    return Built(root, update=update)


def build_hud(w, manager) -> Built:
    view = w.view
    w.status_strip.set_multiline(True)
    status = HudPanel(view, w.status_strip, passive=True)
    menu = HudPanel(view, w.chk_thr, w.chk_spot_info, w.chk_pat_info, w.chk_points)
    menu.hide()
    b_over = QPushButton("Overlays"); b_over.setCheckable(True)
    b_over.setToolTip("what is drawn on the image")
    mark_always(b_over)                 # display only
    zoom = HudPanel(view, w.box_zoom, row(w.b_snap, b_over, None))
    w.lab_z.setAlignment(Qt.AlignCenter)
    rocker = HudPanel(view, w.b_z_up, w.lab_z, w.z_step, w.b_z_dn, w.b_af, w.b_kill)
    jog = HudPanel(view, w.card_jog)
    switches = HudPanel(view, w.chk_track, w.chk_stab, w.chk_click)
    panels = (status, zoom, menu, rocker, jog, switches)

    def place():
        W, H, m = view.width(), view.height(), 8
        zoom.adjustSize(); zoom.move(W - zoom.width() - m, m)
        status.setMaximumWidth(max(120, W - zoom.width() - 3 * m))
        status.adjustSize(); status.move(m, m)
        below = zoom.y() + zoom.height() + 4
        if not menu.isHidden():
            menu.adjustSize(); menu.move(W - menu.width() - m, below)
            below = menu.y() + menu.height() + 4
        jog.adjustSize(); jog.move(W - jog.width() - m, H - jog.height() - m)
        switches.adjustSize(); switches.move(m, H - switches.height() - m)
        rocker.adjustSize()
        x, y = W - rocker.width() - m, max(below + 2, (H - rocker.height()) // 2)
        if y + rocker.height() > jog.y() - 6:          # too short: beside the jog pad
            x = jog.x() - rocker.width() - 6
            y = max(below + 2, H - rocker.height() - m)
        rocker.move(x, y)
        for p in panels:
            p.raise_()

    def show_menu(on):
        menu.setVisible(on)
        place()
    b_over.toggled.connect(show_menu)
    menu._reveal = lambda: b_over.setChecked(True)
    placer = _HudPlacer(view, place)

    # everything else: a slim tab bar under the image, one page at a time
    focus = _hbox(card("Z", row("Z target", w.z_spin, w.b_setz), w.b_datum_z),
                  card("Autofocus", _af_state_row(w), w.chk_cont),
                  card("AF position", row(w.b_af_here, w.b_af_at), w.lab_afpos),
                  None, spacing=8)
    lock = _hbox(_pattern_card(w, with_switch=False), _stab_card(w, with_switch=False),
                 None, spacing=8)
    stage = _hbox(col(w.stage_bar_top, w.card_position), w.card_goto, None, spacing=8)
    bar = tabs(("Lock", scroll(lock)), ("Focus / AF", scroll(focus)),
               ("Stage", scroll(stage)), ("Laser", scroll(_hbox(w.card_laser, None))),
               ("Scan", scroll(w.page_scanning)), ("Alignment", w.page_accuracy))
    bar.setMaximumHeight(280)
    bar.setMinimumHeight(170)
    root = col(w.fault_bar, (view, 1), bar, spacing=4)

    def update(_s):
        place()

    def teardown():
        view.removeEventFilter(placer)
        placer.deleteLater()

    place()
    return Built(root, extras=panels, update=update, teardown=teardown)


def build_split(w) -> Built:
    left = SideColumn(col(
        card("Focus (Z)",
             row("Z target", (w.z_spin, 1), w.b_setz), w.lab_z,
             row("step", (w.z_step, 1)),
             row(w.b_z_up, w.b_z_dn), w.b_datum_z),
        card("Autofocus", row(w.b_af, w.b_kill), _af_state_row(w), w.chk_cont),
        card("AF position", w.b_af_here, w.b_af_at, w.lab_afpos),
        None))
    side = tabs(("Track", _top(_pattern_card(w))), ("Stabilise", _top(_stab_card(w))),
                ("Laser", _top(w.card_laser)))
    # where the stage is and where to send it side by side, the jog pad under
    # them, then the tabs (in one column the tabs were below the screen)
    right = SideColumn(col(_hbox(w.card_position, w.card_goto, spacing=6), w.card_jog,
                           side, None))
    top = _three_columns(left, _image_column(w), right)
    return Built(_vsplit(top, _bottom(w)))


# --------------------------------------------------------------------------- #
# Image first: a toolbar that wraps, a foldable drawer
# --------------------------------------------------------------------------- #
class FlowLayout(QLayout):
    """Items left to right, wrapping onto a new line when the width runs out.

    The Image-first toolbar holds about fifteen controls: in one row they need
    more than a 1366 px screen, so a plain row would make the whole window
    wider than the screen. This wraps instead (Qt's "flow layout" example)."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    # the QLayout interface (Qt calls these; takeAt also runs when a control
    # is re-parented away by a layout switch)
    def addItem(self, item):                            # noqa: N802 (Qt name)
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):                                # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):                                # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):                      # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):                        # noqa: N802
        return True

    def heightForWidth(self, width):                    # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):                        # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def _shown(self):
        return [it for it in self._items if not it.isEmpty()]

    def sizeHint(self):                                 # noqa: N802
        """Everything on one line."""
        items = self._shown()
        w = sum(it.sizeHint().width() for it in items)
        w += self.spacing() * max(0, len(items) - 1)
        h = max((it.sizeHint().height() for it in items), default=0)
        return QSize(w, h)

    def minimumSize(self):                              # noqa: N802
        """As narrow as the widest single item."""
        size = QSize()
        for it in self._shown():
            size = size.expandedTo(it.minimumSize())
        return size

    def _arrange(self, rect: QRect, move: bool) -> int:
        x, y, line_h, gap = rect.x(), rect.y(), 0, self.spacing()
        for it in self._shown():
            hint = it.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line_h = rect.x(), y + line_h + gap, 0      # next line
            if move:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


def _chip(*items) -> QFrame:
    """A switch and its light in one rounded frame (a 'toggle chip')."""
    f = QFrame(); f.setObjectName("chip")
    lay = QHBoxLayout(f); lay.setContentsMargins(8, 2, 8, 2); lay.setSpacing(6)
    _fill(lay, items)
    f.setStyleSheet(
        f"QFrame#chip {{ background: {T.COLORS['panel_hi']};"
        f" border: 1px solid {T.COLORS['border']}; border-radius: 11px; }}"
        "QFrame#chip QLabel, QFrame#chip QCheckBox { background: transparent; }")
    return f


def _vline() -> QFrame:
    f = QFrame(); f.setFrameShape(QFrame.VLine); f.setObjectName("sep")
    f.setStyleSheet(f"color: {T.COLORS['border']};")
    return f


class Drawer(QWidget):
    """The Image-first drawer: pages beside a vertical strip of tab names.

    Folded, only the strip stays (about 30 px): the image gets the width.
    Clicking a tab name while folded opens the drawer on that page. The
    pages are the window's controls in the layout's own containers.
    ``on_fold(folded)`` is told every change the USER makes (to keep it)."""

    def __init__(self, pages, folded: bool, on_fold=None):
        super().__init__()
        self.setObjectName("drawer")
        self._on_fold = on_fold
        self.stack = QStackedWidget()
        self.bar = QTabBar()
        self.bar.setShape(QTabBar.RoundedEast)          # names read top to bottom
        self.bar.setExpanding(False)
        self.bar.setDrawBase(False)
        compact = 0
        for title, page, wide in pages:
            if not wide:                                 # wide: scrolls on its own
                page = SideColumn(col(page, None))
                compact = max(compact, page.sizeHint().width())
            self.stack.addWidget(page)
            self.bar.addTab(title)
        # the compact pages share one width (the image does not jump when the
        # tab changes); a wide page (the scan form) may take more
        self.stack.setMinimumWidth(compact + 12)    # (+ what polishing adds)
        _pages_follow_current(self.stack)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._follow)
        self.bar.tabBarClicked.connect(self._tab_clicked)
        # the body is what folds away; reveal() opens it through _reveal
        self.body = QWidget(); self.body.setObjectName("column")
        bl = QVBoxLayout(self.body); bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.stack)
        self.body._reveal = lambda: self.set_folded(False, user=True)
        self.btn = QToolButton(); self.btn.setAutoRaise(True)
        # big enough to be found at a glance (a plain arrow was easy to miss)
        self.btn.setStyleSheet("font-size: 16px; font-weight: 700; padding: 0 6px;")
        mark_always(self.btn)                            # display only: fine for a viewer
        self.btn.clicked.connect(lambda: self.set_folded(not self.folded, user=True))
        strip = QWidget(); strip.setObjectName("column")
        sl = QVBoxLayout(strip); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)
        sl.addWidget(self.btn, 0, Qt.AlignHCenter)
        sl.addWidget(self.bar); sl.addStretch(1)
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        lay.addWidget(self.body); lay.addWidget(strip)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Expanding)
        self.folded = None
        self.set_folded(bool(folded))

    def _follow(self, i: int) -> None:
        if self.bar.currentIndex() != i:
            self.bar.setCurrentIndex(i)

    def _tab_clicked(self, i: int) -> None:
        if self.folded and i >= 0:
            self.set_folded(False, user=True)

    def set_folded(self, folded: bool, user: bool = False) -> None:
        folded = bool(folded)
        if folded == self.folded:
            return
        self.folded = folded
        self.body.setVisible(not folded)
        # the arrow points where the drawer will go
        self.btn.setText("«" if folded else "»")
        self.btn.setToolTip("open the drawer" if folded
                            else "fold the drawer: the image gets the full width")
        self.updateGeometry()
        if user and self._on_fold is not None:
            self._on_fold(folded)


def build_image_first(w, manager) -> Built:
    """B, "Image first" (Lukas, 2026-10-14): the image is the tab.

    Over it a toolbar: the four switches as chips WITH their lights (match,
    stable, AF), Find focus, Kill AF, Snapshot, zoom and an Overlays
    drop-down. Right a drawer (Focus | Lock | Stage | Laser | Scan) that
    folds to a strip. Under it a status line and the latest log line. A
    switch in the toolbar is NOT repeated in the drawer: one place each."""
    view = w.view
    # the overlay switches: a small panel ON the image, opened by "Overlays"
    over = HudPanel(view, w.chk_thr, w.chk_spot_info, w.chk_pat_info, w.chk_points)
    over.hide()
    b_over = QPushButton("Overlays ▾"); b_over.setCheckable(True)
    b_over.setToolTip("what is drawn on the image")
    mark_always(b_over)                     # display only

    def place():
        over.adjustSize()
        over.move(max(0, view.width() - over.width() - 8), 8)
        over.raise_()

    def show_over(on):
        over.setVisible(on)
        place()
    b_over.toggled.connect(show_over)
    over._reveal = lambda: b_over.setChecked(True)
    placer = _HudPlacer(view, place)

    tool = QWidget(); tool.setObjectName("row")
    flow = FlowLayout(tool, spacing=6)
    for it in (_chip(w.chk_track, w.led_match), _chip(w.chk_stab, w.led_stable),
               _chip(w.chk_cont, w.led_af), _chip(w.chk_click),
               _vline(), w.b_af, w.b_kill, w.b_snap, _vline(), w.box_zoom, b_over):
        flow.addWidget(it)

    # the line under the image: the working state, then the newest log line
    # (the whole log stays where it is, under the tabs)
    last = QLabel(""); last.setObjectName("muted")
    last.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # never widens the tab

    def newest_log():
        text = w.log.document().lastBlock().text().strip()
        last.setText(text)
        last.setToolTip(text)
    w.log.textChanged.connect(newest_log)
    newest_log()
    status = StatusLine(w.status_strip, last)

    image = col(w.fault_bar, tool, (view, 1), status, spacing=4)

    # the drawer: everything else, one page at a time; the switches and lights
    # that sit in the toolbar are left out of it
    focus = col(card("Focus (Z)",
                     row("Z target", (w.z_spin, 1), w.b_setz), w.lab_z,
                     row("step", (w.z_step, 1)),
                     row(w.b_z_up, w.b_z_dn), w.b_datum_z),
                card("Autofocus", row("state", w.lab_af, None, "best", w.lab_best)),
                card("AF position", w.b_af_here, w.b_af_at, w.lab_afpos))
    lock = col(card("Pattern tracking", row(w.chk_roi, w.chk_backup), w.box_search,
                    row((w.lab_patterns, 1), w.b_clr_backups),
                    row(w.b_load_pat, w.b_save_pat)),
               card("Stabiliser", w.box_index, w.lab_drive, w.box_stab_tuning, w.lab_stab))
    stage = col(w.stage_bar_top, w.card_position, w.card_jog, w.card_goto)
    # the scan pages: both scroll (a narrow drawer must not cut the form off)
    scan = tabs(("Define scanning", scroll(w.page_scanning)),
                ("Alignment", scroll(w.page_accuracy)))
    drawer = Drawer([("Focus", focus, False), ("Lock", lock, False),
                     ("Stage", stage, False), ("Laser", w.card_laser, False),
                     ("Scan", scan, True)],
                    folded=bool(getattr(w.cfg.ui, "drawer_collapsed", False)),
                    on_fold=lambda f: w.set_drawer_collapsed(f))
    root = _hbox((image, 1), (drawer, 0), spacing=6)
    manager.drawer = drawer

    def teardown():
        view.removeEventFilter(placer)
        placer.deleteLater()
        try:
            w.log.textChanged.disconnect(newest_log)
        except (RuntimeError, TypeError):
            pass

    place()
    def update(_s):
        status._arrange()            # the status text changed length

    return Built(root, extras=(over,), update=update, teardown=teardown)


BUILDERS = {
    "classic": lambda w, m: build_classic(w),
    "cockpit": lambda w, m: build_cockpit(w),
    "workflow": build_workflow,
    "hud": build_hud,
    "split": lambda w, m: build_split(w),
    "image_first": build_image_first,
}


# --------------------------------------------------------------------------- #
# switching
# --------------------------------------------------------------------------- #
def _explicitly_hidden(widget: QWidget) -> bool:
    """Hidden by the code that owns it (fault banner, "who drives it" line),
    not merely because its container is: that state must survive a move."""
    return (widget.testAttribute(Qt.WA_WState_Hidden)
            and widget.testAttribute(Qt.WA_WState_ExplicitShowHide))


class LayoutManager:
    """Puts the window's Camera-tab controls into the chosen layout."""

    def __init__(self, win, host_layout: QVBoxLayout):
        self.win = win
        self.host = host_layout
        # where the controls wait while one layout is torn down and the next
        # one built -- a hidden child, so they never become windows of their own
        self.parking = QWidget(host_layout.parentWidget())
        self.parking.hide()
        self.name: str | None = None
        self.built: Built | None = None
        self.stepper: Stepper | None = None
        self.drawer: Drawer | None = None         # Image first's drawer
        self._hidden: dict = {}

    def pieces(self) -> dict:
        return {n: getattr(self.win, n) for n in PIECES}

    def apply(self, name: str) -> str:
        name = (name or "").lower()
        if name not in BUILDERS:
            name = "classic"
        if name == self.name and self.built is not None:
            return name
        pieces = self.pieces()
        for n, p in pieces.items():           # first build: every piece; later: placed ones
            if self.built is None or p.parentWidget() is not self.parking:
                # a tab page / step page is hidden by ITS STACK, not by its owner
                self._hidden[n] = (not isinstance(p.parentWidget(), QStackedWidget)
                                   and _explicitly_hidden(p))
        old = self.built
        taken = []
        if old is not None:
            # A scroll area keeps an EVENT FILTER on its content and resizes
            # it to its own viewport -- even after the content has moved
            # elsewhere. A torn-down scroll area and the new layout's then
            # resize the same page back and forth for ever (a stack overflow,
            # found switching Workflow -> Cockpit). takeWidget() lets go.
            for c in [old.root] + old.extras:
                areas = c.findChildren(QScrollArea)
                if isinstance(c, QScrollArea):
                    areas.append(c)
                for a in areas:
                    t = a.takeWidget()
                    if t is not None:
                        taken.append(t)
        for p in pieces.values():
            p.setParent(self.parking)
        if old is not None:
            if old.teardown is not None:
                old.teardown()
            piece_ids = {id(p) for p in pieces.values()}
            for c in [old.root] + old.extras + [t for t in taken if id(t) not in piece_ids]:
                c.hide()
                c.setParent(None)              # out of the window now ...
                c.deleteLater()                # ... and gone at the next event loop
        self.stepper = None
        self.drawer = None
        # what a layout may have changed on a control's LOOK goes back to the
        # default first (the next layout sets its own)
        st = self.win.status_strip
        st.set_multiline(False)
        st.setWordWrap(False)                  # (Image first wraps it)
        st.setMinimumWidth(0); st.setMaximumWidth(16777215)
        self.win.lab_z.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        built = BUILDERS[name](self.win, self)
        self.host.addWidget(built.root)
        for n, p in pieces.items():
            if p.parentWidget() is self.parking:
                p.hide()
            elif isinstance(p.parentWidget(), QStackedWidget):
                pass                           # its stack shows the current page only
            else:
                # setParent hid it; show it again unless its owner had hidden it
                p.setVisible(not self._hidden.get(n, False))
        built.root.show()
        self.built, self.name = built, name
        return name

    def update(self, s) -> None:
        if self.built is not None and self.built.update is not None:
            self.built.update(s)

    def placed(self, widget: QWidget) -> bool:
        """True if ``widget`` sits in the active layout (not parked)."""
        p = widget
        while p is not None:
            if p is self.parking:
                return False
            if self.built is not None and (p is self.built.root or p in self.built.extras):
                return True
            p = p.parentWidget()
        return False

    def reveal(self, widget: QWidget) -> None:
        """Make ``widget`` reachable on screen: select every tab, stack page
        (Workflow step) and pop-up panel on the way to it."""
        child, p = widget, widget.parentWidget()
        while p is not None and p is not self.win:
            if isinstance(p, QStackedWidget):
                tw = p.parentWidget()
                if isinstance(tw, QTabWidget):
                    tw.setCurrentIndex(p.indexOf(child))
                else:
                    p.setCurrentWidget(child)
            fn = getattr(p, "_reveal", None)
            if fn is not None and p.isHidden():
                fn()
            child, p = p, p.parentWidget()
