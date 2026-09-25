"""um <-> steps from the CAMERA calibration (2026-09-16).

The configured `um_per_step` is one datasheet number per axis, the same in both
directions -- which is why a +-2 um jog used to send the same 100 steps
everywhere. When a px/step table has been measured, X and Y take their step
size from it, per direction and at the current drive voltage.
"""

import pytest

from kim import pxcal
from kim.config import Config
from kim.sim_system import build_sim_system

PX_UM = 0.05          # camera pixel size at calibration time, um/px


def _cal(px_x_fwd=0.5, px_x_bwd=0.25, px_y_fwd=0.4, px_y_bwd=0.8):
    """A table with a deliberately asymmetric X and Y, at 85 V and 125 V."""
    table = {}
    for volts in ("85", "125"):
        table[volts] = {
            "X+": [px_x_fwd, 0.0], "X-": [px_x_bwd, 0.0],
            "Y+": [0.0, px_y_fwd], "Y-": [0.0, px_y_bwd],
        }
    return pxcal.PxCalibration(table=table, pixel_size_um=PX_UM)


@pytest.fixture
def brain():
    cfg = Config()
    b, _ = build_sim_system(cfg)
    b.start()
    b._pxcal = _cal()                      # as if a calibration had been loaded
    yield b
    b.shutdown()


def test_step_size_comes_from_the_camera_per_direction(brain):
    assert brain.um_per_step(0, +1) == pytest.approx(0.5 * PX_UM)    # X forward
    assert brain.um_per_step(0, -1) == pytest.approx(0.25 * PX_UM)   # X backward
    assert brain.um_per_step(1, +1) == pytest.approx(0.4 * PX_UM)
    assert brain.um_per_step(1, -1) == pytest.approx(0.8 * PX_UM)
    # no direction (a readout, a speed): the mean of the two
    assert brain.um_per_step(0) == pytest.approx((0.5 + 0.25) / 2 * PX_UM)
    assert brain.um_per_step_source(0) == "camera"


def test_z_keeps_the_configured_value(brain):
    """The camera never measured Z, so it must not inherit X's numbers."""
    assert brain.um_per_step(2, +1) == pytest.approx(brain.cfg.calibration.um_per_step_z)
    assert brain.um_per_step_source(2) == "config"


def test_the_same_jog_forward_and_backward_is_not_the_same_step_count(brain):
    """The headline: +2 um and -2 um differ once the stage is really measured."""
    forward = brain.um_to_steps(0, +2.0, directional=True)
    backward = brain.um_to_steps(0, -2.0, directional=True)
    assert forward == round(2.0 / (0.5 * PX_UM))        # 80 steps out
    assert backward == -round(2.0 / (0.25 * PX_UM))     # 160 steps back
    assert abs(backward) != forward, "the two directions must not collapse to one number"

    # and the verb issues exactly that (the return value is the step TARGET,
    # counted from where the stage is now -- 0 right after the datum)
    brain.zero_counter(0)
    assert brain.move_relative_um(0, +2.0) == forward


def test_configured_values_are_used_when_asked(brain):
    brain.cfg.calibration.use_px_calibration = False
    assert brain.um_per_step(0, +1) == pytest.approx(brain.cfg.calibration.um_per_step_x)
    assert brain.um_per_step_source(0) == "config"
    # the old behaviour: 100 steps for 2 um, whichever way it goes
    assert brain.um_to_steps(0, +2.0, directional=True) == round(2.0 / 0.02)
    assert brain.um_to_steps(0, -2.0, directional=True) == -round(2.0 / 0.02)
    brain.zero_counter(0)
    assert brain.move_relative_um(0, +2.0) == 100


def test_without_a_calibration_nothing_changes():
    cfg = Config()
    b, _ = build_sim_system(cfg)
    b.start()
    try:
        assert b._pxcal is None                       # conftest points it at a temp file
        assert b.um_per_step(0, +1) == pytest.approx(cfg.calibration.um_per_step_x)
        assert b.um_per_step_source(0) == "config"
    finally:
        b.shutdown()


def test_status_reports_both_directions_and_the_source(brain):
    st = brain.status()
    assert st.um_per_step_src == ["camera", "camera", "config"]
    assert st.um_per_step_fwd[0] == pytest.approx(0.5 * PX_UM)
    assert st.um_per_step_bwd[1] == pytest.approx(0.8 * PX_UM)
    # the published mean is what position_um and velocity_um are built from
    assert st.um_per_step[0] == pytest.approx((0.5 + 0.25) / 2 * PX_UM)


def test_the_calibration_report_is_in_micrometres(brain):
    """The table is measured in px/step, but every report says micrometres --
    px/step tells nobody how far the stage moves."""
    rep = brain.get_px_calibration()
    assert rep["um_table"]["85"]["X+"] == pytest.approx(0.5 * PX_UM)
    assert rep["um_table"]["85"]["Y-"] == pytest.approx(0.8 * PX_UM)
    geom = rep["now"]["geometry"]
    assert geom["X"]["um_per_step_fwd"] == pytest.approx(0.5 * PX_UM)
    assert geom["X"]["px_per_step_fwd"] == pytest.approx(0.5)      # still there
    # and what the um <-> steps bridge is using right now, per direction
    assert rep["now"]["um_per_step"]["Y"] == pytest.approx([0.4 * PX_UM, 0.8 * PX_UM])
    assert rep["now"]["source"] == ["camera", "camera", "config"]


def test_a_table_without_a_pixel_size_reports_no_micrometres(brain):
    """An older file has no pixel size: say nothing rather than invent a scale."""
    brain._pxcal.pixel_size_um = 0.0
    rep = brain.get_px_calibration()
    assert rep["um_table"]["85"]["X+"] is None
    assert rep["now"]["geometry"]["X"]["um_per_step_fwd"] is None
    assert brain.um_per_step_source(0) == "config"      # and the bridge falls back


def test_typed_in_step_sizes_without_any_camera():
    """The no-camera path: say how far it steps each way, and the µm verbs obey."""
    cfg = Config()
    b, _ = build_sim_system(cfg)
    b.start()
    try:
        assert b._pxcal is None
        b.set_calibration(0, 0.021, +1)          # forward
        b.set_calibration(0, 0.015, -1)          # backward
        assert b.um_per_step(0, +1) == pytest.approx(0.021)
        assert b.um_per_step(0, -1) == pytest.approx(0.015)
        assert b.um_per_step(0) == pytest.approx((0.021 + 0.015) / 2)   # readouts
        assert b.um_to_steps(0, +2.0, directional=True) == round(2.0 / 0.021)
        assert b.um_to_steps(0, -2.0, directional=True) == -round(2.0 / 0.015)

        # one number again: direction 0 sets both and clears the backward value
        b.set_calibration(0, 0.02)
        assert cfg.calibration.um_per_step_x_bwd == 0.0
        assert b.um_per_step(0, -1) == pytest.approx(0.02)
    finally:
        b.shutdown()


def test_typed_step_sizes_survive_the_ini(tmp_path):
    cfg = Config()
    cfg.calibration.um_per_step_y = 0.0189
    cfg.calibration.um_per_step_y_bwd = 0.0319
    from kim.config import load_config, save_config

    path = tmp_path / "kim.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))
    assert back.calibration.um_per_step_y == pytest.approx(0.0189)
    assert back.calibration.um_per_step_y_bwd == pytest.approx(0.0319)


def test_a_camera_table_wins_over_typed_values(brain):
    """Both exist: the measurement wins, and the config is still there to fall
    back on when use_px_calibration is switched off."""
    brain.set_calibration(0, 0.03, +1)
    assert brain.um_per_step(0, +1) == pytest.approx(0.5 * PX_UM)   # camera
    brain.cfg.calibration.use_px_calibration = False
    assert brain.um_per_step(0, +1) == pytest.approx(0.03)          # typed


def test_voltage_moves_the_step_size(brain):
    """The table is per voltage, and the drive voltage is a live setting."""
    brain._pxcal = _cal()
    brain._pxcal.table["125"]["X+"] = [1.0, 0.0]        # bigger steps at 125 V
    brain.set_voltage(0, 125.0)
    assert brain.um_per_step(0, +1) == pytest.approx(1.0 * PX_UM)
    brain.set_voltage(0, 85.0)
    assert brain.um_per_step(0, +1) == pytest.approx(0.5 * PX_UM)
