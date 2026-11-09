"""Bugs found in kim-control's deep cleaning of 2026-09-28, kept as regression
tests on the single focus axis (Z) of a KIM001.

Each test failed on kim-control's code before its fix; the docstring says what
went wrong. Network tests use ports 19444..19447 (never the service's
5637/5638, and not the ports of the other test files).
"""

from __future__ import annotations

import os
import time

import pytest

from kim001.backends.kinesis_kim001 import KinesisKim
from kim001.backends.sim import SimKim
from kim001.config import Config
from kim001.kim001 import Kim
from kim001.sim_system import build_sim_system

from test_kinesis_backend import FakeKim001

CMD, PUB = 19444, 19445
Z = 2


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


def test_stream_stamp_sits_in_the_middle_of_the_read():
    """kim-control stamped a row AFTER its reads, filing a moving axis late.
    Here each row is one read (only Z exists) and its stamp is that read's
    middle."""
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
        mid = 0.5 * (reads[i][0] + reads[i][1])
        assert abs(t - mid) < 0.012, f"row {i}: stamp {t - mid:+.3f} s from the middle"


# --------------------------------------------------------------------------- #
# 2. set_calibration(+1) must leave the backward step size alone
# --------------------------------------------------------------------------- #
def test_setting_forward_only_keeps_the_backward_step_size():
    """With one number for both ways (backward = 0 = "same"), telling the module
    the FORWARD step is 30 nm silently made the backward step 30 nm too --
    `set_calibration(axis, v, +1)` is documented as "forward only". The camera's
    Z step calibration writes +1 and -1 one after the other."""
    brain, _ = build_sim_system(Config())
    brain.start()
    try:
        assert brain.um_per_step(Z, -1) == pytest.approx(0.02)
        brain.set_calibration(Z, 0.03, +1)
        assert brain.um_per_step(Z, +1) == pytest.approx(0.03)
        assert brain.um_per_step(Z, -1) == pytest.approx(0.02)   # unchanged
        brain.set_calibration(Z, 0.05, -1)
        assert brain.um_per_step(Z, +1) == pytest.approx(0.03)
        assert brain.um_per_step(Z, -1) == pytest.approx(0.05)
        # direction 0 is still "one number again"
        brain.set_calibration(Z, 0.04, 0)
        assert brain.um_per_step(Z, +1) == brain.um_per_step(Z, -1) == pytest.approx(0.04)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. the drive voltage in cfg = the one the controller accepted
# --------------------------------------------------------------------------- #
def _kinesis_brain():
    cfg = Config()
    be = KinesisKim(cfg)
    be._dev = FakeKim001()          # no pylablib, no open(): the fake is the link
    brain = Kim(be, cfg)
    brain._connected = True
    return brain, be._dev


def test_voltage_in_cfg_follows_what_the_controller_accepted():
    """The K-Cube takes whole volts. The backend used to TRUNCATE (99.7 -> 99)
    while the brain kept 99.7 in cfg. Now the backend rounds and the brain keeps
    the value read back from the controller."""
    brain, dev = _kinesis_brain()
    v = brain.set_voltage(Z, 99.7)
    assert dev.drive[1].max_voltage == 100
    assert brain.cfg.motion.voltage_z == pytest.approx(100.0)
    assert v == pytest.approx(100.0)
    assert brain.status().voltage[Z] == pytest.approx(brain.cfg.motion.voltage_z)


# --------------------------------------------------------------------------- #
# 4. goto_position sends nothing for an axis that is already there
# --------------------------------------------------------------------------- #
def test_goto_sends_no_move_when_the_focus_is_already_there():
    """kim-control: a zero-length move re-enabled a channel pair and cut off
    other moves. On a KIM001 there is no pair, but a move nobody needs is still
    not sent."""
    brain, dev = _kinesis_brain()
    brain.store_position(0, "here")               # (0, 0, 183)
    n = dev.calls.count("move_to")
    assert brain.goto_position(0) == [0, 0, 183]
    assert dev.calls.count("move_to") == n
    dev.pos[1] = 183 + 500                        # the focus has since moved
    assert brain.goto_position(0) == [0, 0, 183]
    assert dev.pos[1] == 183 and dev.calls.count("move_to") == n + 1


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
    """kim-control: typing the leash in um converted with the datasheet 20 nm
    while the service used another step size -- "+-100 um" armed a box of a
    different size. The box must be re-shown, and converted back, with the
    mean step size the brain uses (here 25 nm: 30 up, 20 down)."""
    from kim001.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_calibration(Z, 0.03, +1)
    brain.set_calibration(Z, 0.02, -1)
    win = MainWindow(brain, cfg, remote=False)
    try:
        win._unit.setCurrentText("µm")
        win._refresh()
        # Apply WITHOUT typing must not resize the box
        win._apply_leash()
        assert cfg.limits.leash_z == 50000
        win._leash_on.setChecked(True)
        win._leash_z.setValue(10.0)
        win._apply_leash()
        assert brain.um_per_step(Z) == pytest.approx(0.025)
        assert cfg.limits.leash_z == round(10.0 / 0.025)       # 400 steps
        assert brain.status().limit_hi[Z] == 400
    finally:
        win.close()
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 6. remote GUI: Settings OK must not push a stale copy of the config
# --------------------------------------------------------------------------- #
def test_remote_settings_do_not_revert_what_changed_on_the_service(qapp, monkeypatch):
    """A remote GUI keeps a MIRROR of the service's config, fetched once at
    launch. Settings OK pushed that whole mirror with set_config, so anything
    changed on the service since -- 'Zero here', a Z step size written by the
    camera's calibration -- was silently put back (gotcha #5)."""
    from kim001.apps import gui as gui_mod
    from kim001.net.client import Kim001Client
    from kim001.net.protocol import apply_config_dict
    from kim001.net.service import Kim001Service

    class _OkDialog:                      # Settings opened and OK pressed at once
        def __init__(self, cfg, parent=None):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(gui_mod, "SettingsDialog", _OkDialog)

    brain, _ = build_sim_system(Config())
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = Kim001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    mirror = Config()
    apply_config_dict(mirror, cli.get_config())
    win = gui_mod.MainWindow(cli, mirror, remote=True)
    try:
        # changed on the SERVICE after the GUI started (the camera, a script)
        cli.set_step_rate("Z", 2000)
        cli.move_to_step("Z", 400)
        t_end = time.monotonic() + 5
        while brain.status().moving[Z] and time.monotonic() < t_end:
            time.sleep(0.02)
        cli.set_zero("Z")
        cli.set_calibration("Z", 0.031, 0)
        assert brain.cfg.relative.rel_z == 400

        win._open_settings()

        assert brain.cfg.relative.rel_z == 400
        assert brain.cfg.calibration.um_per_step_z == pytest.approx(0.031)
    finally:
        win.close()
        cli.close()
        svc.stop()
        time.sleep(0.1)
