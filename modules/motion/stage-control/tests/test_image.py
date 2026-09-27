"""Pure image<->stage mapping + calibration math (no Qt needed)."""

import math

import pytest

from stage.apps.image_pane import (
    SampleCalibration,
    compute_scale,
    image_to_stage,
    stage_to_image,
)


def test_compute_scale():
    # a 100 px line that is 2 mm long -> 0.02 mm/px
    assert compute_scale((0, 0), (100, 0), 2.0) == pytest.approx(0.02)
    assert compute_scale((0, 0), (30, 40), 5.0) == pytest.approx(0.1)  # 3-4-5 -> 50 px


def test_compute_scale_rejects_degenerate():
    with pytest.raises(ValueError):
        compute_scale((10, 10), (10, 10), 1.0)   # zero-length line
    with pytest.raises(ValueError):
        compute_scale((0, 0), (10, 0), 0.0)       # non-positive length


def test_anchor_maps_to_current_position():
    # The line's start pixel maps exactly to the pinned stage position.
    cal = SampleCalibration(
        scale_mm_per_px=0.01, anchor_px=50, anchor_py=60,
        anchor_x=3.0, anchor_y=4.0, flip_x=False, flip_y=True, calibrated=True,
    )
    x, y = image_to_stage(cal, 50, 60)
    assert x == pytest.approx(3.0)
    assert y == pytest.approx(4.0)


def test_flip_y_makes_up_positive():
    cal = SampleCalibration(
        scale_mm_per_px=0.01, anchor_px=0, anchor_py=0,
        anchor_x=0.0, anchor_y=0.0, flip_y=True, calibrated=True,
    )
    # moving DOWN in the image (py +100) should DECREASE stage Y
    _, y_down = image_to_stage(cal, 0, 100)
    assert y_down == pytest.approx(-1.0)
    # moving RIGHT increases stage X
    x_right, _ = image_to_stage(cal, 100, 0)
    assert x_right == pytest.approx(1.0)


@pytest.mark.parametrize("flip_x,flip_y", [(False, False), (True, False), (False, True), (True, True)])
def test_roundtrip(flip_x, flip_y):
    cal = SampleCalibration(
        scale_mm_per_px=0.037, anchor_px=123, anchor_py=45,
        anchor_x=-2.5, anchor_y=7.25, flip_x=flip_x, flip_y=flip_y, calibrated=True,
    )
    for px, py in [(0, 0), (200, 350), (123, 45), (500, 10)]:
        x, y = image_to_stage(cal, px, py)
        bpx, bpy = stage_to_image(cal, x, y)
        assert bpx == pytest.approx(px)
        assert bpy == pytest.approx(py)


def test_stage_to_image_needs_calibration():
    with pytest.raises(ValueError):
        stage_to_image(SampleCalibration(), 1.0, 2.0)  # scale 0
