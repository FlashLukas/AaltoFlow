"""Edited step amplitudes are not overwritten before Apply (2026-10-03).

The same bug as signalhound's (Lukas, 2026-10-01: "whenever I change any
settings it comes back to the original ones"): the status poll rewrote every
amplitude box that did not have keyboard focus, while the values are sent
only by "Apply amplitudes" -- so X forward typed in reverted as soon as the
user clicked into X backward, and Apply then sent the old value.
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
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(qapp):
    from agilis.apps.gui import MainWindow
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    try:
        yield win, brain
    finally:
        win.close()
        brain.shutdown()


def _wait_for(pred, timeout=2.0):
    """The brain's poll thread publishes status at 50 Hz: wait for it."""
    t0 = time.monotonic()
    while not pred() and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    return pred()


def test_amplitude_edits_survive_the_poll_until_apply(window):
    win, brain = window
    win._refresh()
    (xf, xb), (yf, yb) = win._amp_boxes
    old_f, old_b = brain.status().amplitude_fwd[0], brain.status().amplitude_bwd[0]
    new_f, new_b = (old_f % 40) + 5, (old_b % 40) + 7
    xf.setValue(new_f)
    xb.setValue(new_b)
    for _ in range(3):
        win._refresh()                              # the status poll runs meanwhile
        time.sleep(0.03)
    assert xf.value() == new_f and xb.value() == new_b
    assert xf in win._dirty and "border" in xf.styleSheet()
    assert brain.status().amplitude_fwd[0] == old_f     # nothing sent yet

    win._apply_amplitudes()                         # BOTH reach the controller
    assert _wait_for(lambda: brain.status().amplitude_fwd[0] == new_f
                     and brain.status().amplitude_bwd[0] == new_b)
    assert not win._dirty and xf.styleSheet() == ""

    # after Apply the boxes follow changes made elsewhere again
    brain.set_amplitude(1, 12, +1)
    assert _wait_for(lambda: brain.status().amplitude_fwd[1] == 12)
    win._refresh()
    assert yf.value() == 12

    # Enter applies; the force sync drops an unsent edit
    yb.setValue(9)
    yb.lineEdit().returnPressed.emit()
    assert _wait_for(lambda: brain.status().amplitude_bwd[1] == 9)
    assert not win._dirty
    yb.setValue(20)
    win._sync_amplitudes(brain.status(), force=True)
    assert yb.value() == 9 and not win._dirty
