"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise skipped, so the headless core suite is unaffected).

Builds the window against the simulator offscreen, refreshes it, drives every
control and paints the PhaseDial -- enough to catch import errors, layout
crashes and signal-wiring mistakes without a display."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dsphase.config import Config
from dsphase.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(app, brain, pred, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout and not pred(brain.status()):
        app.processEvents()
        time.sleep(0.01)


def test_window_builds_and_drives(app):
    from dsphase.apps.gui import MainWindow
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    win = MainWindow(brain, cfg)
    win.resize(1120, 700)
    win._refresh()
    assert win.phase_value.text() not in ("", "-")

    win.phase_spin.setValue(45.3); win._set_phase()
    win.att_spin.setValue(6.0); win._set_attenuation()
    win.freq_spin.setValue(5000.0); win._set_frequency()
    win._goto_phase(-90)
    _settle(app, brain, lambda s: s.phase_deg == -90.0)
    win._nudge(+1)
    _settle(app, brain, lambda s: s.phase_deg == -89.5)
    assert brain.status().phase_deg == -89.5
    win._toggle_output()
    _settle(app, brain, lambda s: s.output_on)
    win._refresh()
    assert brain.status().output_on is True
    assert win.state_badge.text() == "OUTPUT ON"

    # paint the dial in both states; the animation tick must not throw
    win.dial.resize(700, 240)
    win.dial._tick()
    win.dial.grab()
    brain.set_output(False)
    _settle(app, brain, lambda s: not s.output_on)
    win._refresh()
    win.dial.grab()
    win.close()
    assert brain.status().connected is False


def test_settings_dialog_applies(app):
    from dsphase.apps.settings_dialog import SettingsDialog
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        applied = []
        dlg = SettingsDialog(brain, cfg, lambda: applied.append(True))
        dlg.w[("limits", "att_min_dB")].setValue(5.0)
        dlg.w[("device", "phase_step_deg")].setValue(5.625)
        dlg._apply_and_close()
        assert cfg.limits.att_min_dB == 5.0
        assert cfg.device.phase_step_deg == 5.625
        assert applied == [True]
        _settle(app, brain, lambda s: s.attenuation_dB == 10.0)
    finally:
        brain.shutdown()


def test_input_boxes_start_from_the_units_state(app):
    """The service adopts the unit's state, so the GUI's boxes must show it:
    pressing Set next to an untouched box must not change the RF."""
    from dsphase.apps.gui import MainWindow
    cfg = Config()
    brain, backend = build_sim_system(cfg, phase_deg=-45.0, attenuation_dB=17.25)
    win = MainWindow(brain, cfg)
    try:
        _settle(app, brain, lambda s: s.adopted)
        win._refresh()
        assert win.phase_spin.value() == -45.0
        assert win.att_spin.value() == 17.25
        assert backend.write_log == []
    finally:
        win.close()
