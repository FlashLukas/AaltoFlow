"""Bugs found in the deep cleaning of 2026-09-28, one test per bug.

Each test failed on the code as it was before the fix; the docstring says what
went wrong and why it mattered on the rig. Network tests use ports 15740..15743
(never the service's 5567/5568, and not the ports of the other test files).
"""

from __future__ import annotations

import os
import time

import pytest

from kim import pxcal
from kim.backends.kinesis_kim import KinesisKim
from kim.backends.sim import SimKim
from kim.config import Config
from kim.kim import Kim
from kim.sim_system import build_sim_system

from test_kinesis_backend import FakeKim101

CMD, PUB = 15740, 15741


def _camera_table():
    """X steps 25 nm forward / 12.5 nm back (mean 18.75 nm): NOT the 20 nm
    config value, so a conversion done with the config shows up."""
    return pxcal.PxCalibration(
        table={"85": {"X+": [0.5, 0.0], "X-": [0.25, 0.0],
                      "Y+": [0.0, 0.4], "Y-": [0.0, 0.8]}},
        pixel_size_um=0.05)


# --------------------------------------------------------------------------- #
# 1. stream time stamp
# --------------------------------------------------------------------------- #
class _SlowReads(SimKim):
    """A sim whose position reads take 20 ms each, like USB round trips, and
    which remembers WHEN each read happened."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.read_times: list[tuple[float, float]] = []

    def read_position(self, axis):
        t0 = time.time()
        time.sleep(0.02)
        v = super().read_position(axis)
        self.read_times.append((t0, time.time()))
        return v


def test_stream_stamp_sits_in_the_middle_of_the_three_reads():
    """The sampler reads X, Y, Z one after the other and stamps the row. The
    comment said the stamp sits in the middle of the reads; it was taken AFTER
    the last one -- 1.5 reads late for the axis in the middle, and 2.5 reads
    late for X, the axis a fly scan along X bins by. On the rig each read is a
    USB round trip, so a moving stage was filed a few ms (= a fraction of a
    pixel at fly speed) ahead of where it was read."""
    cfg = Config()
    backend = _SlowReads(cfg)
    brain = Kim(backend, cfg)
    brain.start()
    try:
        backend.read_times.clear()
        brain.stream_start(rate_hz=5)
        time.sleep(0.5)
        chunk = brain.stream_stop()
    finally:
        brain.shutdown()
    stamps = chunk["t"]
    assert len(stamps) >= 2
    reads = backend.read_times
    for i, t in enumerate(stamps):
        window = reads[3 * i: 3 * i + 3]
        mid = 0.5 * (window[0][0] + window[-1][1])
        assert abs(t - mid) < 0.012, f"row {i}: stamp {t - mid:+.3f} s from the middle"


# --------------------------------------------------------------------------- #
# 2. set_calibration(+1) must leave the backward step size alone
# --------------------------------------------------------------------------- #
def test_setting_forward_only_keeps_the_backward_step_size():
    """With one number for both ways (backward = 0 = "same"), telling the module
    the FORWARD step is 30 nm silently made the backward step 30 nm too --
    `set_calibration(axis, v, +1)` is documented as "forward only"."""
    cfg = Config()
    cfg.calibration.use_px_calibration = False
    brain, _ = build_sim_system(cfg)
    brain.start()
    try:
        assert brain.um_per_step(0, -1) == pytest.approx(0.02)
        brain.set_calibration(0, 0.03, +1)
        assert brain.um_per_step(0, +1) == pytest.approx(0.03)
        assert brain.um_per_step(0, -1) == pytest.approx(0.02)   # unchanged
        # and the other way round still works as before
        brain.set_calibration(1, 0.05, -1)
        assert brain.um_per_step(1, +1) == pytest.approx(0.02)
        assert brain.um_per_step(1, -1) == pytest.approx(0.05)
        # direction 0 is still "one number again"
        brain.set_calibration(0, 0.04, 0)
        assert brain.um_per_step(0, +1) == brain.um_per_step(0, -1) == pytest.approx(0.04)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. the drive voltage the um bridge uses = the one the controller accepted
# --------------------------------------------------------------------------- #
def _kinesis_brain():
    cfg = Config()
    be = KinesisKim(cfg)
    be._dev = FakeKim101()          # no pylablib, no open(): the fake is the link
    brain = Kim(be, cfg)
    brain._connected = True
    return brain, be._dev


def test_voltage_in_cfg_follows_what_the_controller_accepted():
    """The KIM101 takes whole volts. The backend used to TRUNCATE (99.7 -> 99)
    while the brain kept 99.7 in cfg -- and cfg's voltage is what picks the
    row of the camera px/step table, so um were converted at a voltage the
    stage was not running at. Now the backend rounds and the brain keeps the
    value read back from the controller."""
    brain, dev = _kinesis_brain()
    v = brain.set_voltage(0, 99.7)
    assert dev.drive[1].max_voltage == 100
    assert brain.cfg.motion.voltage_x == pytest.approx(100.0)
    assert v == pytest.approx(100.0)
    assert brain.status().voltage[0] == pytest.approx(brain.cfg.motion.voltage_x)


# --------------------------------------------------------------------------- #
# 4. goto_position must not re-enable the Z pair for a Z that is already there
# --------------------------------------------------------------------------- #
def test_goto_does_not_cut_off_xy_with_a_zero_length_z_move():
    """The KIM101 drives one channel pair at a time: (1,2) = X,Y or (3,4) = Z.
    goto_position commanded X, Y and then Z unconditionally, so even when Z was
    already at the stored count its move enabled (3,4) and stopped the X/Y
    moves just started (2026-09-14 pair model). An axis already at its target
    and at rest is now left alone."""
    brain, dev = _kinesis_brain()
    brain.store_position(0, "here")               # (31, 667, 183)
    dev.pos[1] = 31 + 500                         # X has since moved
    dev.pos[2] = 667 - 300                        # and Y
    dev.enabled, dev.cut_off = (), []
    targets = brain.goto_position(0)
    assert targets == [31, 667, 183]
    assert dev.pos[1] == 31 and dev.pos[2] == 667
    assert dev.enabled == (1, 2)                  # Z's pair never enabled
    assert dev.cut_off == []


# --------------------------------------------------------------------------- #
# 5. GUI leash in um: converted with the step size in force
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_gui_leash_in_um_uses_the_live_step_size(qapp):
    """Typing the leash in um converted with cfg's datasheet 20 nm even when
    the camera calibration was in charge (X mean 18.75 nm here) -- so "+-100 um"
    armed a +-5000-step box that is really +-93.75 um, and the hint line right
    under it (which uses the live value) said so. The jog was fixed for this on
    2026-09-16; the leash box was missed."""
    from kim.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain._pxcal = _camera_table()
    win = MainWindow(brain, cfg, remote=False)
    try:
        win._unit.setCurrentText("µm")    # boxes first shown with cfg's 20 nm...
        win._refresh()                    # ...then the live 18.75 nm arrives
        # Apply WITHOUT typing must not resize the box: the boxes are re-shown
        # with the live step size, the same one Apply converts back with.
        win._apply_leash()
        assert cfg.limits.leash_xy == 50000
        win._leash_on.setChecked(True)
        win._leash_xy.setValue(100.0)
        win._leash_z.setValue(10.0)
        win._apply_leash()
        assert cfg.limits.leash_xy == round(100.0 / brain.um_per_step(0))   # 5333
        assert cfg.limits.leash_z == round(10.0 / brain.um_per_step(2))
    finally:
        win.close()
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 6. remote GUI: Settings OK must not push a stale copy of the config
# --------------------------------------------------------------------------- #
def test_remote_settings_do_not_revert_what_changed_on_the_service(qapp, monkeypatch):
    """A remote GUI keeps a MIRROR of the service's config, fetched once at
    launch. Settings OK pushed that whole mirror with set_config, so anything
    changed on the service since -- 'Zero here' (the display origin), step
    sizes typed on the STEP SIZE card, a leash set by a script -- was silently
    put back to the launch-time values (gotcha #5, from our own GUI)."""
    from kim.apps import gui as gui_mod
    from kim.net.client import KimClient
    from kim.net.protocol import apply_config_dict
    from kim.net.service import KimService

    class _OkDialog:                      # Settings opened and OK pressed at once
        def __init__(self, cfg, parent=None):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(gui_mod, "SettingsDialog", _OkDialog)

    scfg = Config()
    scfg.calibration.use_px_calibration = False
    brain, _ = build_sim_system(scfg)
    svc = KimService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = KimClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    mirror = Config()
    apply_config_dict(mirror, cli.get_config())
    win = gui_mod.MainWindow(cli, mirror, remote=True)
    try:
        # changed on the SERVICE after the GUI started (another client, a script)
        cli.move_to_step("X", 400)
        t_end = time.monotonic() + 5
        while brain.status().moving[0] and time.monotonic() < t_end:
            time.sleep(0.02)
        cli.set_zero("X")
        cli.set_calibration("Z", 0.031, 0)
        assert brain.cfg.relative.rel_x == 400

        win._open_settings()

        assert brain.cfg.relative.rel_x == 400
        assert brain.cfg.calibration.um_per_step_z == pytest.approx(0.031)
    finally:
        win.close()
        cli.close()
        svc.stop()
        time.sleep(0.1)
