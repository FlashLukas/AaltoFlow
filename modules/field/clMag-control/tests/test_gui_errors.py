"""The GUI must not look calm while the hardware is failing (2026-09-28).

With a failed Hall read the status keeps the LAST GOOD field, so the old
panel went on showing a green "STABLE" on a frozen number. Now status
`hw_error` turns the lamp red and shows the text. Offscreen, no service.
"""

import os
from dataclasses import replace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from clMag.apps.gui import MainWindow  # noqa: E402
from clMag.config import Config  # noqa: E402
from clMag.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(app, **status_changes):
    cfg = Config()
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg)
    win = MainWindow(ctrl, cfg, cal)
    win.timer.stop()                       # we call _refresh ourselves
    base = ctrl.status()
    ctrl.status = lambda: replace(base, **status_changes)
    win._refresh()
    return win


def test_hw_error_turns_the_lamp_red_and_shows_the_text(app):
    win = _window(app, state="STABLE", field_stable=True,
                  hw_error="Hall probe: OSError: DAQmx read failed")
    assert "HARDWARE ERROR" in win.stable_dot.text()
    assert not win.error_label.isHidden()
    assert "DAQmx" in win.error_label.text()
    win.close()


def test_drifted_out_of_tolerance_is_not_shown_as_seeking(app):
    win = _window(app, state="STABLE", field_stable=False)
    assert "out of tolerance" in win.stable_dot.text()
    assert win.error_label.isHidden()
    win.close()


def test_loop_error_is_shown(app):
    win = _window(app, state="IDLE", loop_error="control loop error: IOError: x")
    assert "control loop error" in win.error_label.text()
    assert not win.error_label.isHidden()
    win.close()
