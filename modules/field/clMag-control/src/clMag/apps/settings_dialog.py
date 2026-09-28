"""Settings dialog: edit every tunable, plus load/save config and calibration.

This is the Python version of your LabVIEW "Settings" and "Calibration" panes.
Nothing here talks to hardware directly -- it edits the shared `cfg` object in
place and then calls `controller.apply_config()` so the running control loop
picks the changes up. Values are grouped into tabs:

  Hardware      -- Kepco VISA address, DAQ channel/terminal/range (used once the
                   real hardware backends are wired in; the simulator ignores them)
  Hall probe    -- the volts->field conversion (this is how you calibrate the
                   field *measurement*)
  Control       -- ramp, PID, limits, stabilizer, acquisition profiles
  Calibration   -- save/load the measured B(I) curve; "Run Calibration" on the
                   main window measures a fresh one
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from PySide6 import QtWidgets

from ..config import Config
from ..calibration import FieldCalibration
from .theme import COLORS
from .calibration_viewer import CalibrationViewer, can_view


# ---- little widget builders ------------------------------------------------

def _dspin(value, lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _ispin(value, lo, hi, suffix=""):
    w = QtWidgets.QSpinBox()
    w.setRange(lo, hi); w.setValue(int(value))
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _hint(text):
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


def _section(form: QtWidgets.QFormLayout, title: str):
    lbl = QtWidgets.QLabel(title.upper())
    lbl.setStyleSheet(f"color:{COLORS['muted']}; font-weight:700; letter-spacing:1px; margin-top:6px;")
    form.addRow(lbl)


def _copy_config_into(dst: Config, src: Config) -> None:
    """Copy every field from src into dst's existing sub-objects IN PLACE, so
    shared references (the acquisition thread's hall/profiles) stay valid."""
    for group in ("hall", "ramp", "pid", "limits", "stabilizer", "acquisition", "hardware"):
        d, s = getattr(dst, group), getattr(src, group)
        for f in dataclass_fields(d):
            setattr(d, f.name, getattr(s, f.name))


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, controller, cfg: Config, on_applied, parent=None):
        super().__init__(parent)
        self.ctrl = controller
        self.cfg = cfg
        self.on_applied = on_applied
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        self.w = {}   # (group, field) -> widget

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._hardware_tab(), "Hardware")
        tabs.addTab(self._hall_tab(), "Hall probe")
        tabs.addTab(self._control_tab(), "Control")
        tabs.addTab(self._calibration_tab(), "Calibration")
        tabs.addTab(self._appearance_tab(), "Appearance")

        # bottom bar: config file ops on the left, OK/Cancel on the right
        bar = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load config…"); load_btn.clicked.connect(self._load_config)
        save_btn = QtWidgets.QPushButton("Save config…"); save_btn.clicked.connect(self._save_config)
        bar.addWidget(load_btn); bar.addWidget(save_btn); bar.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_and_close)
        bar.addWidget(cancel); bar.addWidget(apply)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(tabs)
        root.addLayout(bar)

    # ---- tabs ------------------------------------------------------------

    def _form_widget(self):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setLabelAlignment(QtWidgets.Qt.AlignRight if False else form.labelAlignment())
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)
        return page, form

    def _add(self, form, group, field, label, widget):
        self.w[(group, field)] = widget
        form.addRow(label, widget)

    def _hardware_tab(self):
        page, form = self._form_widget()
        hw = self.cfg.hardware
        self._add(form, "hardware", "kepco_visa", "Kepco VISA address",
                  QtWidgets.QLineEdit(hw.kepco_visa))
        self._add(form, "hardware", "daq_channel", "DAQ channel",
                  QtWidgets.QLineEdit(hw.daq_channel))
        combo = QtWidgets.QComboBox(); combo.addItems(["RSE", "NRSE", "DIFF"])
        combo.setCurrentText(hw.daq_terminal)
        self._add(form, "hardware", "daq_terminal", "DAQ terminal", combo)
        self._add(form, "hardware", "daq_v_min", "DAQ V min",
                  _dspin(hw.daq_v_min, -10, 10, 2, 0.5, "V"))
        self._add(form, "hardware", "daq_v_max", "DAQ V max",
                  _dspin(hw.daq_v_max, -10, 10, 2, 0.5, "V"))
        form.addRow(_hint("Used when the real pyvisa / nidaqmx backends are enabled. "
                          "The simulator ignores these, but they are saved to config."))
        return page

    def _hall_tab(self):
        page, form = self._form_widget()
        h = self.cfg.hall
        self._add(form, "hall", "sensitivity_mT_per_mV", "Sensitivity",
                  _dspin(h.sensitivity_mT_per_mV, 0, 10, 4, 0.0001, "mT/mV"))
        self._add(form, "hall", "correction", "Correction (geometry)",
                  _dspin(h.correction, 0, 100, 3, 0.001))
        self._add(form, "hall", "offset_mV", "Zero-field offset",
                  _dspin(h.offset_mV, -20000, 20000, 1, 1, "mV"))
        form.addRow(_hint("B[mT] = (V[mV] − offset) × sensitivity × correction. "
                          "These calibrate the field MEASUREMENT. If you change them, "
                          "re-run the B(I) calibration afterwards."))
        return page

    def _control_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget(); form = QtWidgets.QFormLayout(inner)
        form.setSpacing(8); form.setContentsMargins(16, 16, 16, 16)

        r = self.cfg.ramp; p = self.cfg.pid; lim = self.cfg.limits
        st = self.cfg.stabilizer; ac = self.cfg.acquisition

        _section(form, "Ramp")
        self._add(form, "ramp", "increment_A", "Increment", _dspin(r.increment_A, 0.0001, 1, 4, 0.001, "A"))
        self._add(form, "ramp", "delay_s", "Step delay", _dspin(r.delay_s, 0.001, 1, 3, 0.001, "s"))

        _section(form, "PID (PI)")
        self._add(form, "pid", "Kc_A_per_mT", "Kc", _dspin(p.Kc_A_per_mT, 0, 1, 4, 0.001, "A/mT"))
        self._add(form, "pid", "Ti_s", "Ti (integral time)", _dspin(p.Ti_s, 0.01, 100, 2, 0.05, "s"))
        self._add(form, "pid", "Td_s", "Td (derivative time)", _dspin(p.Td_s, 0, 100, 2, 0.05, "s"))

        _section(form, "Limits")
        self._add(form, "limits", "current_max_A", "Current limit", _dspin(lim.current_max_A, 0, 20, 2, 0.1, "A"))
        self._add(form, "limits", "field_tolerance_mT", "Field tolerance", _dspin(lim.field_tolerance_mT, 0.001, 10, 3, 0.01, "mT"))
        self._add(form, "limits", "field_step_mT", "Seek undershoot (field step)", _dspin(lim.field_step_mT, 0, 50, 2, 0.5, "mT"))
        self._add(form, "limits", "stable_time_s", "Stable dwell", _dspin(lim.stable_time_s, 0, 10, 2, 0.1, "s"))

        _section(form, "Stabilizer")
        self._add(form, "stabilizer", "gain_A_per_mT", "Gain", _dspin(st.gain_A_per_mT, 0, 1, 4, 0.0001, "A/mT"))

        _section(form, "Acquisition")
        self._add(form, "acquisition", "precise_samples", "Precise samples", _ispin(ac.precise_samples, 1, 1000000))
        self._add(form, "acquisition", "precise_rate_Hz", "Precise rate", _dspin(ac.precise_rate_Hz, 1, 2_000_000, 0, 1000, "Hz"))
        self._add(form, "acquisition", "fast_samples", "Fast samples", _ispin(ac.fast_samples, 1, 1000000))
        self._add(form, "acquisition", "fast_rate_Hz", "Fast rate", _dspin(ac.fast_rate_Hz, 1, 2_000_000, 0, 1000, "Hz"))

        scroll.setWidget(inner); outer.addWidget(scroll)
        return page

    def _calibration_tab(self):
        page, form = self._form_widget()
        self._cal_summary = QtWidgets.QLabel()
        self._refresh_cal_summary()
        form.addRow("Current curve", self._cal_summary)
        row = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save…"); save.clicked.connect(self._save_cal)
        load = QtWidgets.QPushButton("Load…"); load.clicked.connect(self._load_cal)
        view = QtWidgets.QPushButton("View curve…"); view.clicked.connect(self._view_cal)
        row.addWidget(save); row.addWidget(load); row.addWidget(view)
        holder = QtWidgets.QWidget(); holder.setLayout(row)
        form.addRow(holder)
        form.addRow(_hint("The calibration file also stores the Hall parameters it was taken with. "
                          "When you load a curve you can choose whether to adopt those parameters. "
                          "To measure a new curve, use “Run Calibration” on the main window."))
        return page

    def _appearance_tab(self):
        page, form = self._form_widget()
        combo = QtWidgets.QComboBox(); combo.addItems(["dark", "light"])
        combo.setCurrentText(self.cfg.ui.theme)
        self._add(form, "ui", "theme", "Theme", combo)
        form.addRow(_hint("Applies on the next launch. Use Save config… to keep it, "
                          "or override once with  run_gui.py --theme light."))
        return page

    def _refresh_cal_summary(self):
        cal = self.ctrl.get_calibration()
        if cal and getattr(cal, "currents_A", None):
            lo, hi = cal.range_mT
            self._cal_summary.setText(f"{len(cal.currents_A)} points, {lo:.1f} … {hi:.1f} mT")
        else:
            self._cal_summary.setText("none loaded")

    # ---- read widgets back into cfg -------------------------------------

    def _pull_into_cfg(self):
        for (group, field), widget in self.w.items():
            target = getattr(self.cfg, group)
            if isinstance(widget, QtWidgets.QLineEdit):
                setattr(target, field, widget.text().strip())
            elif isinstance(widget, QtWidgets.QComboBox):
                setattr(target, field, widget.currentText())
            elif isinstance(widget, QtWidgets.QSpinBox):
                setattr(target, field, int(widget.value()))
            else:  # QDoubleSpinBox
                setattr(target, field, float(widget.value()))

    def _refresh_widgets_from_cfg(self):
        for (group, field), widget in self.w.items():
            val = getattr(getattr(self.cfg, group), field)
            if isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(str(val))
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentText(str(val))
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.setValue(val)

    # ---- actions ---------------------------------------------------------

    def _apply_and_close(self):
        self._pull_into_cfg()
        self.ctrl.apply_config()
        self.on_applied()
        self.accept()

    def _save_config(self):
        self._pull_into_cfg()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save config", "config.ini", "Config (*.ini)")
        if path:
            self.cfg.save(path)

    def _load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load config", "", "Config (*.ini)")
        if path:
            loaded = Config.load(path)
            _copy_config_into(self.cfg, loaded)
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()

    def _save_cal(self):
        cal = self.ctrl.get_calibration()
        if not cal or not getattr(cal, "currents_A", None):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save calibration", "calibration.txt", "Text (*.txt)")
        if path:
            cal.save(path)

    def _load_cal(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load calibration", "", "Text (*.txt)")
        if not path:
            return
        cal = FieldCalibration.load(path)
        self.ctrl.set_calibration(cal)         # local: sets it; remote: pushes to service

        # ask whether to ALSO adopt the Hall parameters saved with this curve
        h = cal.hall
        choice = QtWidgets.QMessageBox.question(
            self, "Adopt Hall parameters?",
            "This calibration was recorded with:\n\n"
            f"    sensitivity  {h.sensitivity_mT_per_mV:.4f} mT/mV\n"
            f"    correction   {h.correction:.3f}\n"
            f"    offset       {h.offset_mV:.1f} mV\n\n"
            "Load these Hall-probe parameters into the live configuration too?\n"
            "(Choose No to keep your current parameters and just view the curve as recorded.)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if choice == QtWidgets.QMessageBox.Yes:
            self.cfg.hall.sensitivity_mT_per_mV = h.sensitivity_mT_per_mV
            self.cfg.hall.correction = h.correction
            self.cfg.hall.offset_mV = h.offset_mV
            self._refresh_widgets_from_cfg()
            self.ctrl.apply_config()           # push the adopted Hall params (matters for remote)

        self._refresh_cal_summary()
        self.on_applied()
        # show the curve as recorded
        if can_view(cal):
            CalibrationViewer(cal, parent=self,
                              title=f"Calibration curve · {path.split('/')[-1]}").exec()

    def _view_cal(self):
        cal = self.ctrl.get_calibration()
        if not can_view(cal):
            QtWidgets.QMessageBox.information(
                self, "No curve to show",
                "There is no calibration curve loaded to view. Load one, or run a "
                "calibration from the main window.")
            return
        CalibrationViewer(cal, parent=self).exec()
