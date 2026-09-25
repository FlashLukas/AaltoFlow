"""Sample navigator core: design loading and design->stage registration.

The registration is checked against a KNOWN transform: make stage positions
from a chosen rotation/scale/offset, hand the points to Registration, and
require it to find that transform again.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from scan_core.navigator import (
    Registration, image_design, load_gds, load_session, save_session,
    unit_factor, waypoints,
)


def _truth(rot_deg, scale=(1.0, 1.0), mirror=False, t=(0.0, 0.0), skew_deg=0.0):
    a = math.radians(rot_deg)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    S = np.diag(scale)
    K = np.array([[1.0, math.tan(math.radians(skew_deg))], [0.0, 1.0]])
    F = np.diag([1.0, -1.0 if mirror else 1.0])
    M = R @ K @ S @ F
    return lambda d: tuple(M @ np.asarray(d, float) + np.asarray(t, float))


DESIGN_PTS = [(-2000.0, -1500.0), (1800.0, 1900.0), (1700.0, -2100.0),
              (-1900.0, 2000.0), (100.0, 50.0)]


def test_prior_only_uses_rotation_and_mirror():
    r = Registration(rotation_deg=90.0)
    assert r.to_stage(100.0, 0.0) == pytest.approx((0.0, 100.0), abs=1e-9)
    r.mirror = True
    assert r.to_stage(0.0, 100.0) == pytest.approx((100.0, 0.0), abs=1e-9)


def test_one_point_keeps_the_prior_rotation_and_sets_the_offset():
    truth = _truth(30.0, t=(5000.0, -3000.0))
    r = Registration(rotation_deg=30.0)
    r.add_point(DESIGN_PTS[0], truth(DESIGN_PTS[0]))
    for d in DESIGN_PTS:
        assert r.to_stage(*d) == pytest.approx(truth(d), abs=1e-6)


def test_two_points_find_rotation_and_scale_whatever_the_prior():
    # the prior is wrong on purpose: two points must override it
    truth = _truth(-12.5, scale=(0.97, 0.97), t=(1234.0, 567.0))
    r = Registration(rotation_deg=40.0)
    for d in DESIGN_PTS[:2]:
        r.add_point(d, truth(d))
    for d in DESIGN_PTS:
        assert r.to_stage(*d) == pytest.approx(truth(d), abs=1e-6)
    s = r.summary()
    assert s["rotation_deg"] == pytest.approx(-12.5)
    assert s["scale_x"] == pytest.approx(0.97)


def test_three_points_detect_a_mirror():
    truth = _truth(20.0, mirror=True, t=(10.0, 20.0))
    r = Registration(mirror=False)
    for d in DESIGN_PTS[:3]:
        r.add_point(d, truth(d))
    assert r.mirror is True
    assert max(r.residuals()) < 1e-6
    assert r.summary()["mirror"] is True


def test_three_points_report_a_residual_when_the_stage_is_not_square():
    # unequal X/Y scale (KIM X and Y step sizes differ): a similarity cannot
    # fit it, and the residual says so ...
    truth = _truth(5.0, scale=(1.00, 0.95), t=(0.0, 0.0))
    r = Registration()
    for d in DESIGN_PTS[:3]:
        r.add_point(d, truth(d))
    assert r.fitted_model() == "similarity"
    assert max(r.residuals()) > 10.0
    # ... and from four points the affine fit takes it out.
    r.add_point(DESIGN_PTS[3], truth(DESIGN_PTS[3]))
    assert r.fitted_model() == "affine"
    assert max(r.residuals()) < 1e-6
    s = r.summary()
    assert s["scale_x"] == pytest.approx(1.00)
    assert s["scale_y"] == pytest.approx(0.95)


def test_inverse_round_trips():
    truth = _truth(33.0, scale=(1.02, 0.98), skew_deg=0.5, t=(-400.0, 900.0))
    r = Registration()
    for d in DESIGN_PTS[:4]:
        r.add_point(d, truth(d))
    for d in DESIGN_PTS:
        assert r.to_design(*r.to_stage(*d)) == pytest.approx(d, abs=1e-6)


def test_correct_offset_moves_everything_and_later_points_stay_consistent():
    truth = _truth(15.0, t=(100.0, 200.0))
    r = Registration()
    for d in DESIGN_PTS[:2]:
        r.add_point(d, truth(d))
    # the stage counter drifted by (+26, -7) um: every true position now reads
    # that much higher than before
    drift = np.array([26.0, -7.0])
    drifted = lambda d: tuple(np.asarray(truth(d)) + drift)
    applied = r.correct_offset(DESIGN_PTS[4], drifted(DESIGN_PTS[4]))
    assert applied == pytest.approx(tuple(drift), abs=1e-6)
    for d in DESIGN_PTS:
        assert r.to_stage(*d) == pytest.approx(drifted(d), abs=1e-6)
    # a reference point added AFTER the drift must not bend the fit
    r.add_point(DESIGN_PTS[2], drifted(DESIGN_PTS[2]))
    assert max(r.residuals()) < 1e-6
    assert r.to_stage(*DESIGN_PTS[3]) == pytest.approx(drifted(DESIGN_PTS[3]), abs=1e-6)


def test_points_on_one_spot_are_refused():
    r = Registration()
    r.add_point((0, 0), (0, 0))
    r.add_point((0, 0), (5, 5))
    with pytest.raises(ValueError):
        r.transform()


def test_waypoints_approach_from_one_side():
    assert waypoints((100, 100), (0, 0)) == [(100.0, 100.0)]
    assert waypoints((100, 100), (0, 0), 20) == [(80.0, 80.0), (100.0, 100.0)]
    # already just below the target: go straight
    assert waypoints((100, 100), (90, 95), 20) == [(100.0, 100.0)]
    # above it: the detour is needed
    assert waypoints((100, 100), (105, 95), 20) == [(80.0, 80.0), (100.0, 100.0)]


def test_units():
    assert unit_factor("mm") == 1000.0
    assert unit_factor("um") == 1.0
    assert unit_factor("furlong") is None


def test_image_design_is_centred_with_the_given_width():
    d = image_design("x.png", 2000, 1000, 4000.0)
    assert d.um_per_px == pytest.approx(2.0)
    assert d.bbox() == pytest.approx((-2000, -1000, 2000, 1000))
    assert d.image_pixel_to_design(0, 0) == pytest.approx((-2000, 1000))   # top-left
    assert d.image_pixel_to_design(2000, 1000) == pytest.approx((2000, -1000))


def test_session_round_trip(tmp_path):
    r = Registration(rotation_deg=12.0, model="similarity")
    r.add_point((1, 2), (3, 4))
    r.correct_offset((1, 2), (5, 5))
    d = image_design("sample.jpg", 100, 50, 1000.0)
    p = tmp_path / "s.nav.json"
    save_session(p, d, r, extra={"stage": "kim."})
    back = load_session(p)
    assert back["stage"] == "kim."
    assert back["design"]["width_um"] == 1000.0
    r2 = back["registration"]
    assert r2.to_stage(10, 20) == pytest.approx(r.to_stage(10, 20))


def test_load_gds_flattens_references_and_skips_klayout_context(tmp_path):
    gdstk = pytest.importorskip("gdstk")
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    ctx = lib.new_cell("$$$CONTEXT_INFO$$$")          # KLayout's bookkeeping cell
    ctx.add(gdstk.rectangle((0, 0), (1, 1), layer=99))
    unit = lib.new_cell("DOT")
    unit.add(gdstk.rectangle((0, 0), (10, 10), layer=43))
    top = lib.new_cell("TOP")
    top.add(gdstk.Reference(unit, (0, 0), columns=3, rows=2, spacing=(100, 100)))
    top.add(gdstk.rectangle((-2300, -2300), (2300, -2200), layer=4))
    path = tmp_path / "t.gds"
    lib.write_gds(str(path))

    d = load_gds(path)
    assert d.cell == "TOP"
    assert list(d.layers) == ["4/0", "43/0"]
    assert len(d.layers["43/0"].polygons) == 6               # 3 x 2 array flattened
    assert d.bbox() == pytest.approx((-2300, -2300, 2300, 110))


def test_stage_pairs_are_found_by_name_with_their_units():
    from scan_core.navigator import stage_pairs
    from scan_core.registry import Registry, Settable

    reg = Registry()
    for pid, unit in (("kim.position_x", "um"), ("kim.position_y", "um"),
                      ("kim.position_z", "um"), ("stage.position_x", "mm"),
                      ("stage.position_y", "mm"), ("piezo.position_x", "um")):
        reg.add(Settable(pid, pid, unit, (-10, 10), lambda v: None, lambda: 0.0))
    pairs = {p.label: p for p in stage_pairs(reg)}
    assert set(pairs) == {"kim", "stage"}          # piezo has no Y here
    assert pairs["stage"].um_per_unit == 1000.0
    assert pairs["stage"].limits_um() == (-10000, -10000, 10000, 10000)
    assert pairs["stage"].inside(9999, -9999) and not pairs["stage"].inside(10001, 0)


def test_the_simulator_offers_a_stage():
    from scan_core import build_sim_registry
    from scan_core.navigator import stage_pairs
    (p,) = stage_pairs(build_sim_registry())
    assert p.label == "simulator" and p.um_per_unit == 1.0
