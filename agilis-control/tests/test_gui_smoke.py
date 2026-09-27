"""GUI smoke test: build offscreen, refresh, drive the cards, paint the indicator.

Skipped automatically if PySide6 isn't installed.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from agilis.config import Config  # noqa: E402
from agilis.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp):
    from agilis.apps import theme
    from agilis.apps.gui import MainWindow

    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    brain.start()
    qapp.setStyleSheet(theme.build_stylesheet())
    win = MainWindow(brain, cfg, remote=False)
    try:
        yield win, brain, sim
    finally:
        win.close()
        brain.shutdown()


def _settle(brain, axis):
    time.sleep(0.1)
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < 4:
        time.sleep(0.02)
    time.sleep(0.08)


def test_moves_in_both_languages(window):
    win, brain, _ = window
    win._unit.setCurrentText("steps")
    win._rel_mode.setChecked(False)
    win._target[1].setValue(300)
    win._move(1)
    _settle(brain, 1)
    assert brain.status().position_steps[1] == 300
    win._unit.setCurrentText("µm")
    win._rel_mode.setChecked(True)
    win._target[0].setValue(5.0)
    win._move(0)                              # +5 um = 100 steps
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 100
    win._refresh()
    assert win._big[0].text() == "5.000"


def test_step_buttons_follow_the_unit(window):
    win, brain, _ = window
    win._unit.setCurrentText("steps")
    win._jog_step.setValue(40)
    assert win._jog_buttons[0][1].text() == "+ 40 st"
    win._unit.setCurrentText("µm")            # 40 steps x 50 nm = 2 um
    assert win._jog_step.value() == pytest.approx(2.0)
    win._step(0, -1)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == -40


def test_hold_to_jog_press_and_release(window):
    win, brain, sim = window
    win._jog_press(0, +1)
    assert win._keepalive.isActive()
    time.sleep(0.1)
    assert sim.axis_state(1) == 2             # jogging
    win._keep_jogging()
    win._jog_release(0)
    assert sim.axis_state(1) == 0 and not win._keepalive.isActive()


def test_amplitude_card_and_preset(window):
    win, brain, sim = window
    f, b = win._amp_boxes[1]
    f.setValue(25)
    b.setValue(9)
    win._apply_amplitudes()
    assert sim.read_amplitude(2, +1) == 25 and sim.read_amplitude(2, -1) == 9
    time.sleep(0.08)
    win._refresh()
    assert "Y +25 / -9" in win._amp_hint.text()
    assert "approximate" in win._cal_hint.text()
    win._steps_btn.setChecked(True)
    assert sim.read_amplitude(1, -1) == 50
    assert win._steps_btn.text() == "Steps: Large"


def test_step_size_card_stores_per_direction(window):
    win, brain, _ = window
    fwd, bwd = win._cal_boxes[0]
    fwd.setValue(0.061)
    bwd.setValue(0.047)
    win._apply_step_sizes()
    assert brain.um_per_step(0, +1) == pytest.approx(0.061)
    assert brain.um_per_step(0, -1) == pytest.approx(0.047)


def test_leash_card(window):
    win, brain, _ = window
    win._leash_on.setChecked(True)
    win._leash_steps.setValue(500)
    win._apply_leash()
    time.sleep(0.08)
    win._refresh()
    assert brain.status().leash is True
    assert win._indicator._half(0) == 500
    assert "ARMED" in win._leash_hint.text()


def test_positions_and_log(window):
    win, brain, _ = window
    win._store_current()
    assert win._table.item(0, 1).text() == "P00"
    brain._emit("warn", "hello")


def test_indicator_paints_in_both_themes(qapp):
    from PySide6.QtGui import QPixmap

    from agilis.apps import theme
    from agilis.apps.gui import StickSlipIndicator

    for name in ("dark", "light"):
        theme.set_theme(name)
        ind = StickSlipIndicator(Config())
        ind.resize(560, 240)
        ind.set_state([5000, -3000], [True, False], [1, -1], [16, 30], [16, 12],
                      [-20000, -20000], [20000, 20000], True, [False, True], [True, False])
        ind._tick()
        pm = QPixmap(ind.size())
        ind.render(pm)
        assert not pm.isNull()
    theme.set_theme("dark")


def test_settings_dialog_builds(qapp):
    from agilis.apps.settings_dialog import SettingsDialog

    assert SettingsDialog(Config()) is not None
