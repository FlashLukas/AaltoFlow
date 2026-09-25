"""The front panel (§7 of the guide): MainWindow + the signature indicator.

PySide6, Fusion style, dark/amber theme.  ``run_app(ctrl, cfg, remote=False)``
builds the window; ``ctrl`` is either a local :class:`Stage` brain or a
:class:`StageClient` -- they share a method surface, so the GUI is agnostic.

Layout:
  * a top bar with a Settings… button (all configuration lives in a SEPARATE
    window, not embedded here),
  * a horizontal splitter: LEFT = operation controls (read-outs + indicator,
    move/jog/home/zero, the position list, the log); RIGHT = the sample
    overview image pane (load, rotate, calibrate, click-to-move).

Configuration -- velocities, limits, offsets, the 2x2 transform, hardware -- is
edited in the Settings dialog and pushed to the controller on accept.

Threading rule: instrument events arrive on a background thread, so they MUST
cross into Qt through a signal -- see :class:`Bridge`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, axis_limits
from . import theme
from .image_pane import ImagePane
from .settings_dialog import SettingsDialog
from .theme import repolish

AXES = ("X", "Y", "Z")


# --------------------------------------------------------------------------- #
# event bridge (thread boundary -> Qt)
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    event = Signal(str, str)  # (level, message)


# --------------------------------------------------------------------------- #
# small UI helpers
# --------------------------------------------------------------------------- #
def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    cap = QLabel(title)
    cap.setObjectName("cardTitle")
    lay.addWidget(cap)
    return frame, lay


def _spin(value=0.0, lo=-1e6, hi=1e6, step=0.1, decimals=4) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setButtonSymbols(QDoubleSpinBox.NoButtons)
    s.setMinimumWidth(70)
    return s


# --------------------------------------------------------------------------- #
# signature indicator widget
# --------------------------------------------------------------------------- #
class StageIndicator(QWidget):
    """Top-down XY map of the travel envelope with a live marker + a Z bar.

    Marker pulses amber (its own ~33 ms QTimer, independent of the status poll)
    while any axis moves; green/red dots show which axes are homed.
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setMinimumHeight(210)
        self._pos = [0.0, 0.0, 0.0]
        self._moving = [False, False, False]
        self._homed = [False, False, False]
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, pos, moving, homed) -> None:
        self._pos = list(pos)
        self._moving = list(moving)
        self._homed = list(homed)
        if any(moving):
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        self.update()

    def _frac(self, axis: int) -> float:
        lo, hi = axis_limits(self.cfg, axis)
        if hi <= lo:
            return 0.5
        val = self._pos[axis]
        if val != val:  # NaN
            return 0.5
        return min(1.0, max(0.0, (val - lo) / (hi - lo)))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        margin = 16
        z_bar_w = 26
        plot = _Rect(margin, margin, w - margin * 3 - z_bar_w, h - margin * 2)

        p.setPen(QPen(QColor(theme.COLORS["border"]), 1))
        p.setBrush(QColor(theme.COLORS["code_bg"]))
        p.drawRoundedRect(plot.x, plot.y, plot.w, plot.h, 8, 8)

        p.setPen(QPen(QColor(theme.COLORS["grid"]), 1))
        for i in range(1, 4):
            gx = plot.x + plot.w * i / 4
            gy = plot.y + plot.h * i / 4
            p.drawLine(int(gx), plot.y, int(gx), plot.y + plot.h)
            p.drawLine(plot.x, int(gy), plot.x + plot.w, int(gy))

        fx = self._frac(0)
        fy = self._frac(1)
        mx = plot.x + fx * plot.w
        my = plot.y + (1.0 - fy) * plot.h

        moving = any(self._moving)
        base = QColor(theme.COLORS["accent"]) if moving else QColor(theme.COLORS["accent_dim"])

        p.setPen(QPen(QColor(theme.COLORS["muted"]), 1, Qt.DashLine))
        p.drawLine(plot.x, int(my), plot.x + plot.w, int(my))
        p.drawLine(int(mx), plot.y, int(mx), plot.y + plot.h)

        if moving:
            r = 10 + 6 * (0.5 + 0.5 * math.sin(self._phase))
            halo = QColor(theme.COLORS["accent"])
            halo.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(int(mx - r), int(my - r), int(2 * r), int(2 * r))

        p.setPen(QPen(QColor(theme.COLORS["accent_hi"]), 2))
        p.setBrush(base)
        p.drawEllipse(int(mx - 6), int(my - 6), 12, 12)

        zx = plot.x + plot.w + margin
        p.setPen(QPen(QColor(theme.COLORS["border"]), 1))
        p.setBrush(QColor(theme.COLORS["code_bg"]))
        p.drawRoundedRect(zx, plot.y, z_bar_w, plot.h, 6, 6)
        fz = self._frac(2)
        fill_h = plot.h * fz
        zcol = QColor(theme.COLORS["accent"]) if self._moving[2] else QColor(theme.COLORS["ok"])
        p.setPen(Qt.NoPen)
        p.setBrush(zcol)
        p.drawRoundedRect(zx + 3, int(plot.y + plot.h - fill_h) + 1,
                          z_bar_w - 6, int(fill_h) - 2 if fill_h > 2 else 1, 4, 4)
        p.setPen(QColor(theme.COLORS["muted"]))
        p.drawText(zx - 2, plot.y + plot.h + 13, "Z")

        p.setPen(QColor(theme.COLORS["muted"]))
        p.drawText(plot.x, plot.y + plot.h + 13, "X →")
        p.save()
        p.translate(plot.x - 4, plot.y + plot.h)
        p.rotate(-90)
        p.drawText(0, 0, "Y →")
        p.restore()

        for a in range(3):
            dot = QColor(theme.COLORS["ok"]) if self._homed[a] else QColor(theme.COLORS["danger"])
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(plot.x + 6 + a * 16, plot.y + 6, 8, 8)
        p.end()


class _Rect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self.remote = remote
        self.setWindowTitle("3D Coarse Stage" + ("  [remote]" if remote else ""))
        self.resize(1300, 800)

        self._bridge = Bridge()
        self._bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda level, msg: self._bridge.event.emit(level, msg)

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

        self._reload_positions()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # top bar
        top = QHBoxLayout()
        title = QLabel("3D COARSE STAGE")
        title.setObjectName("cardTitle")
        top.addWidget(title)
        top.addStretch(1)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        # body: splitter [controls | image pane]
        splitter = QSplitter(Qt.Horizontal)

        controls = QWidget()
        col = QVBoxLayout(controls)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        col.addWidget(self._build_readout_card())
        col.addWidget(self._build_move_card())
        col.addWidget(self._build_positions_card(), 1)
        col.addWidget(self._build_log_card(), 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(controls)
        scroll.setMinimumWidth(470)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._image = ImagePane(self.ctrl, self.cfg, log=self._on_event)

        splitter.addWidget(scroll)
        splitter.addWidget(self._image)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([490, 770])
        root.addWidget(splitter, 1)

    def _build_readout_card(self) -> QFrame:
        frame, lay = _card("POSITION  (device · rel · logical)")
        self._big = []
        self._sub_lbl = []
        row = QHBoxLayout()
        for a in range(3):
            col = QVBoxLayout()
            cap = QLabel(AXES[a])
            cap.setObjectName("caption")
            big = QLabel("--")
            big.setObjectName("bigValue")
            sub = QLabel("rel -- · log --")
            sub.setObjectName("muted")
            col.addWidget(cap)
            col.addWidget(big)
            col.addWidget(sub)
            row.addLayout(col)
            self._big.append(big)
            self._sub_lbl.append(sub)
        lay.addLayout(row)

        self._indicator = StageIndicator(self.cfg)
        lay.addWidget(self._indicator)
        return frame

    def _build_move_card(self) -> QFrame:
        frame, lay = _card("MOVE  /  JOG  /  HOME  /  ZERO")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._target = []
        for a in range(3):
            grid.addWidget(QLabel(AXES[a]), a, 0)
            sp = _spin(0.0, -1e6, 1e6, 0.1)
            grid.addWidget(sp, a, 1)
            self._target.append(sp)

            move_btn = QPushButton("Move")
            move_btn.setMaximumWidth(58)
            move_btn.clicked.connect(lambda _c, ax=a: self._move(ax))
            grid.addWidget(move_btn, a, 2)

            minus = QPushButton("−")
            plus = QPushButton("+")
            minus.setMaximumWidth(30)
            plus.setMaximumWidth(30)
            minus.clicked.connect(lambda _c, ax=a: self._jog(ax, -1))
            plus.clicked.connect(lambda _c, ax=a: self._jog(ax, +1))
            grid.addWidget(minus, a, 3)
            grid.addWidget(plus, a, 4)

            zero = QPushButton("Zero")
            zero.setMaximumWidth(52)
            zero.setToolTip("Set this axis' current position as its relative zero")
            zero.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.set_zero(ax)))
            grid.addWidget(zero, a, 5)

            home = QPushButton("Home")
            home.setMaximumWidth(56)
            home.clicked.connect(lambda _c, ax=a: self._do(lambda: self.ctrl.home(ax)))
            grid.addWidget(home, a, 6)
        lay.addLayout(grid)

        mrow = QHBoxLayout()
        self._rel_mode = QCheckBox("Relative mode  (targets measured from the zero)")
        mrow.addWidget(self._rel_mode)
        mrow.addStretch(1)
        lay.addLayout(mrow)

        row = QHBoxLayout()
        row.addWidget(QLabel("jog step"))
        self._jog_step = _spin(self.cfg.motion.jog_step, 0.0, 100.0, 0.1, 3)
        row.addWidget(self._jog_step)
        row.addStretch(1)
        zero_all = QPushButton("Zero all")
        zero_all.clicked.connect(lambda: self._do(self.ctrl.set_zero_all))
        row.addWidget(zero_all)
        home_all = QPushButton("Home all")
        home_all.clicked.connect(lambda: self._do(self.ctrl.home_all))
        row.addWidget(home_all)
        stop = QPushButton("STOP")
        stop.setObjectName("danger")
        repolish(stop)
        stop.clicked.connect(lambda: self._do(self.ctrl.stop_all))
        row.addWidget(stop)
        lay.addLayout(row)

        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("logical u,v,w"))
        self._logical_in = [_spin(0.0, -1e6, 1e6, 0.1) for _ in range(3)]
        for s in self._logical_in:
            lrow.addWidget(s)
        gobtn = QPushButton("Move (logical)")
        gobtn.setObjectName("primary")
        repolish(gobtn)
        gobtn.clicked.connect(
            lambda: self._do(lambda: self.ctrl.move_logical(*[s.value() for s in self._logical_in]))
        )
        lrow.addWidget(gobtn)
        lay.addLayout(lrow)
        return frame

    def _build_positions_card(self) -> QFrame:
        frame, lay = _card("POSITION LIST  (20 slots, device coords)")
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["#", "name", "X", "Y", "Z"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._slot_name = QLineEdit()
        self._slot_name.setPlaceholderText("optional name for selected slot")
        store = QPushButton("Store current")
        store.setObjectName("primary")
        repolish(store)
        goto = QPushButton("Go to")
        clear = QPushButton("Clear")
        store.clicked.connect(self._store_current)
        goto.clicked.connect(self._goto_selected)
        clear.clicked.connect(self._clear_selected)
        row.addWidget(QLabel("name"))
        row.addWidget(self._slot_name, 1)
        row.addWidget(store)
        row.addWidget(goto)
        row.addWidget(clear)
        lay.addLayout(row)

        frow = QHBoxLayout()
        save = QPushButton("Save list…")
        load = QPushButton("Load list…")
        save.clicked.connect(self._save_positions)
        load.clicked.connect(self._load_positions)
        frow.addStretch(1)
        frow.addWidget(save)
        frow.addWidget(load)
        lay.addLayout(frow)
        return frame

    def _build_log_card(self) -> QFrame:
        frame, lay = _card("LOG")
        self._log = QPlainTextEdit()
        self._log.setObjectName("log")
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setMinimumHeight(120)
        lay.addWidget(self._log)
        return frame

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self._push_config()
            # limits may have changed -> refresh the image pane's limit box
            self._image.on_limits_changed()
            self._on_event("info", "settings applied")

    def _push_config(self) -> None:
        """Apply edited config to the running controller (local or remote)."""
        if self.remote:
            from ..net.protocol import config_to_dict
            self._do(lambda: self.ctrl.set_config(config_to_dict(self.cfg)))
        else:
            self._do(self.ctrl.apply_config)

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def _do(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._on_event("error", f"{type(exc).__name__}: {exc}")

    def _move(self, axis: int) -> None:
        val = self._target[axis].value()
        if self._rel_mode.isChecked():
            self._do(lambda: self.ctrl.move_relative(axis, val))
        else:
            self._do(lambda: self.ctrl.move_axis(axis, val))

    def _jog(self, axis: int, sign: int) -> None:
        step = self._jog_step.value()
        current = self.ctrl.status().position[axis]
        if current != current:
            current = 0.0
        self._do(lambda: self.ctrl.move_axis(axis, current + sign * step))

    def _selected_slot(self) -> int:
        row = self._table.currentRow()
        return row if row >= 0 else 0

    def _store_current(self) -> None:
        slot = self._selected_slot()
        name = self._slot_name.text().strip()
        self._do(lambda: self.ctrl.store_position(slot, name))
        self._reload_positions()

    def _goto_selected(self) -> None:
        self._do(lambda: self.ctrl.goto_position(self._selected_slot()))

    def _clear_selected(self) -> None:
        self._do(lambda: self.ctrl.clear_position(self._selected_slot()))
        self._reload_positions()

    def _save_positions(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save position list", "positions.json", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.save_positions(path))

    def _load_positions(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load position list", "", "JSON (*.json)")
        if path:
            self._do(lambda: self.ctrl.load_positions(path))
            self._reload_positions()

    def _reload_positions(self) -> None:
        try:
            positions = self.ctrl.get_positions()
        except Exception:
            positions = []
        self._table.setRowCount(len(positions))
        for i, p in enumerate(positions):
            used = p.get("used", False)
            cells = [
                str(i),
                p.get("name", "") if used else "—",
                f"{p.get('x', 0):.4g}" if used else "",
                f"{p.get('y', 0):.4g}" if used else "",
                f"{p.get('z', 0):.4g}" if used else "",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not used:
                    item.setForeground(QColor(theme.COLORS["muted"]))
                self._table.setItem(i, c, item)

    # ------------------------------------------------------------------ #
    # polling + events
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        st = self.ctrl.status()
        for a in range(3):
            val = st.position[a]
            self._big[a].setText("--" if val != val else f"{val:.4g}")
            rel = st.relative[a]
            lg = st.logical[a]
            rel_txt = "rel --" if rel != rel else f"rel {rel:.4g}"
            log_txt = "log --" if lg != lg else f"log {lg:.4g}"
            self._sub_lbl[a].setText(f"{rel_txt} · {log_txt}")
        self._indicator.set_state(st.position, st.moving, st.homed)
        self._image.update_marker()

    def _on_event(self, level: str, msg: str) -> None:
        color = {"info": theme.COLORS["muted"], "warn": theme.COLORS["accent_hi"], "error": theme.COLORS["danger"]}.get(level, theme.COLORS["text"])
        self._log.appendHtml(f'<span style="color:{color}">[{level}]</span> {msg}')


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_app(ctrl, cfg: Config, remote: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    # Choose the palette BEFORE any widget is built, so every widget and custom
    # paintEvent reads the right COLORS.  Startup-only: there is no live toggle.
    theme.set_theme(getattr(cfg.ui, "theme", "dark"))

    app = QApplication.instance() or QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    theme.apply_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()
