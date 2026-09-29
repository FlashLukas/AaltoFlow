"""Z step calibration by the WIDTH method (Lukas, 2026-09-29).

Why: on the rig (63x, real KIM101 Z) "Calibrate Z steps" refused with "poor
fit (up walk R^2 0.955)". The up walk was LOPSIDED -- sigma^2 fell 136 -> 56
over 4.2 counter um before focus and rose 56 -> 110 over 1.8 after it. The
spot's sigma^2 is symmetric in TRUE Z, so the KIM step size changes WITHIN one
walk; one parabola per walk is the wrong model and its curvature ratio means
little.

The replacement: at the same sigma^2 level in both walks, the counter width
of the down walk divided by that of the up walk is s_up / s_down, whatever the
step does along the way (camera/zcal.py). These tests pin that down on
analytic curves, on the rig's own up walk, and on the simulator with a
position-dependent step size (SimSlipStickZ gain_slope / gain_law).
"""

import math
import time

import numpy as np
import pytest

from camera import zcal as ZC
from camera.backends.sim import SimSlipStickZ
from camera.config import Config, load_config, save_config

from test_z_step_calibration import ZF, _rig, _run_zcal, _true, _wait

# The rig's up walk, 2026-09-29 (counter um -> sigma^2 px^2, 0.25 um levels)
RIG_UP = [(11.0, 172), (11.2, 136), (11.45, 126), (11.7, 114), (11.95, 110), (12.2, 99),
          (12.45, 92), (12.7, 86), (12.95, 82), (13.2, 77), (13.45, 73), (13.7, 67),
          (13.95, 66), (14.2, 64), (14.45, 60), (14.7, 59), (14.95, 59), (15.2, 60),
          (15.45, 56), (15.7, 60), (15.95, 64), (16.2, 70), (16.45, 73), (16.7, 85),
          (16.95, 95), (17.2, 110)]


def _rig_up():
    return (np.array([a for a, _ in RIG_UP], float), np.array([b for _, b in RIG_UP], float))


def _down_from_up(n_up, m_up, q, top=40.0, step=0.25, noise=0.015, seed=1, jump=1.25):
    """The down walk the rig WOULD record if s_up / s_down = q and both
    directions share the step size's position dependence: the up curve
    stretched along the counter by q (a walk with smaller steps needs q x
    more counter over the same true stretch), walked downwards, with a little
    noise and a jumped first level (as the rig's)."""
    span = (n_up[-1] - n_up[0]) * q
    nd = np.arange(top, top - span - 1e-9, -step)
    nu = n_up[-1] - (top - nd) / q                 # the same true height on the up walk
    md = np.interp(nu, n_up, m_up)
    md = md * (1.0 + noise * np.random.default_rng(seed).standard_normal(len(md)))
    md[0] *= jump
    return nd, md


# --------------------------------------------------------------------------- #
# the arithmetic (camera/zcal.py)
# --------------------------------------------------------------------------- #
def test_parse_levels():
    assert ZC.parse_levels("1.3, 1.5; 2.0 ,1.8") == [1.3, 1.5, 1.8, 2.0]
    assert ZC.parse_levels("0.5, 1.0, 1.4") == [1.4]           # <= 1 has no width
    assert ZC.parse_levels([2, 1.5]) == [1.5, 2.0]
    with pytest.raises(ValueError):
        ZC.parse_levels("1.3, abc")


def test_the_smoother_removes_one_outlier_and_keeps_a_valley():
    x = np.linspace(-3, 3, 25)
    y = 50 + 10 * x ** 2
    bad = y.copy()
    bad[8] *= 1.6                                   # one noisy level
    s = ZC.smooth_3rh(bad)
    assert np.max(np.abs(s - y)[1:-1]) < 0.06 * y.max()
    assert abs(s[8] - y[8]) < 0.1 * y[8]
    assert int(np.argmin(s)) == 12


def test_crossings_interpolate_linearly_between_levels():
    n = np.arange(0.0, 11.0)
    m = 10.0 + np.abs(n - 5.0) * 4.0              # a V: 10 at n = 5, slope 4
    sh = ZC.WalkShape(n=n, raw=m, smooth=m, i_min=5, m_min=10.0)
    left, right = ZC.crossings(sh, 20.0)
    assert (left, right) == pytest.approx((2.5, 7.5))
    assert ZC.crossings(sh, 100.0) == (None, None)


def test_walk_shape_leaves_out_the_first_levels_and_sorts_by_counter():
    n, m = _rig_up()
    sh = ZC.walk_shape(n[::-1], m[::-1], skip_first=1)   # a DOWN walk: first = 17.2
    assert sh.n[0] == 11.0 and sh.n[-1] == 16.95
    assert sh.dropped == 1


@pytest.mark.parametrize("q", [0.6, 1.0, 1.43, 2.0])
def test_widths_give_the_ratio_exactly_on_a_lopsided_valley(q):
    """Analytic: true z(n) with a step size that varies along the walk (the
    same law in both directions), sigma^2 a parabola in TRUE z. Every level
    gives the same ratio; the parabola fit in counter units does not fit."""
    def walk(g, direction):
        # dz/dn = g (1 + 0.15 z): z(n) = (exp(0.15 g n) - 1) / 0.15 from z = 0
        n = np.arange(-40.0, 40.0, 0.05) * direction
        z = (np.exp(0.15 * g * n) - 1.0) / 0.15
        return n, 50.0 + 10.0 * (z - 0.3) ** 2
    nu, mu = walk(1.0, +1)
    nd, md = walk(1.0 / q, -1)
    keep_u, keep_d = mu < 200, md < 200
    su = ZC.walk_shape(nu[keep_u], mu[keep_u])
    sd = ZC.walk_shape(nd[keep_d], md[keep_d])
    r = ZC.width_ratio(su, sd, "1.3, 1.5, 1.8, 2.0, 3.0")
    assert r.ok, r.why
    assert len(r.levels) == 5
    for d in r.levels:
        assert d["ratio"] == pytest.approx(q, rel=2e-3)
    assert r.spread < 0.005
    f = ZC.fit_parabola(nu[keep_u], mu[keep_u], 2.0, 3)
    assert f["r2"] < 0.99                         # lopsided in counter units


def test_the_rig_up_walk_is_a_poor_parabola():
    """Reproduces the rig's refusal: the old gate (0.97) refused this walk."""
    n, m = _rig_up()
    f = ZC.fit_parabola(n, m, 2.0, 3, skip_first=1)
    assert 0.93 < f["r2"] < 0.97, f["r2"]


@pytest.mark.parametrize("q", [0.8, 1.43, 2.0])
def test_the_width_method_is_not_fooled_by_the_rig_walk(q):
    n, m = _rig_up()
    nd, md = _down_from_up(n, m, q)
    su = ZC.walk_shape(n, m, skip_first=1)
    sd = ZC.walk_shape(nd, md, skip_first=1)
    r = ZC.width_ratio(su, sd, "1.3, 1.45, 1.55", max_spread=0.10)
    assert r.ok, (r.why, r.summary(), r.detail())
    assert len(r.levels) == 3
    assert r.ratio == pytest.approx(q, rel=0.04), r.summary()
    assert r.spread < 0.10


def test_levels_a_walk_does_not_reach_on_both_sides_are_left_out():
    n, m = _rig_up()
    nd, md = _down_from_up(n, m, 1.43)
    su = ZC.walk_shape(n, m, skip_first=1)
    sd = ZC.walk_shape(nd, md, skip_first=1)
    r = ZC.width_ratio(su, sd, "1.3, 1.5, 2.2, 3.0")
    assert [d["k"] for d in r.levels] == [1.3, 1.5]
    assert [k for k, _ in r.skipped] == [2.2, 3.0]
    assert "not reached" in r.detail()
    r = ZC.width_ratio(su, sd, "2.2, 3.0")
    assert not r.ok and r.why.startswith("too few levels (0 usable, need 2)")


def test_levels_that_disagree_are_refused():
    n, m = _rig_up()
    nd, md = _down_from_up(n, m, 1.43, noise=0.0)
    # a DIRECTION-DEPENDENT nonlinearity: the down walk's steps grow away from
    # focus, the up walk's do not -> the width ratio depends on the level
    centre = 15.0 * 1.43 + (40.0 - 17.2 * 1.43)
    nd = centre + (nd - centre) * (1.0 + 0.3 * np.abs(nd - centre))
    su = ZC.walk_shape(n, m, skip_first=1)
    sd = ZC.walk_shape(nd, md, skip_first=1)
    r = ZC.width_ratio(su, sd, "1.3, 1.45, 1.55", max_spread=0.10)
    assert not r.ok
    assert r.why.startswith("levels disagree")
    assert r.spread > 0.10
    assert len(r.levels) == 3                       # the evidence is kept


# --------------------------------------------------------------------------- #
# the simulator: a position-dependent step size
# --------------------------------------------------------------------------- #
def test_sim_step_size_can_depend_on_the_position():
    uni = SimSlipStickZ(z0=0.0, z_focus=0.0, up_gain=1.0, down_gain=0.7)
    uni.move_counter(2.0)
    assert uni.true_z() == pytest.approx(2.0)       # the old model, unchanged
    z = SimSlipStickZ(z0=0.0, z_focus=0.0, up_gain=1.0, down_gain=0.7, gain_slope=0.1)
    z.move_counter(-3.0)
    below = z.true_z()                              # steps smaller below the reference
    z2 = SimSlipStickZ(z0=0.0, z_focus=0.0, up_gain=1.0, down_gain=0.7, gain_slope=0.1)
    z2.move_counter(3.0)
    above = z2.true_z()
    assert above > 3.0 and -2.1 < below < 0.0
    # dz/dn = g (1 + 0.1 z) -> z = (exp(0.1 g n) - 1) / 0.1
    assert above == pytest.approx((math.exp(0.3) - 1) / 0.1, rel=1e-4)
    law = SimSlipStickZ(z0=0.0, z_focus=0.0, up_gain=1.0, down_gain=1.0,
                        gain_law=lambda zz, d: 2.0 if d < 0 else 1.0)
    law.move_counter(1.0)
    law.move_counter(0.0)
    assert law.true_z() == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# the brain on the simulator
# --------------------------------------------------------------------------- #
def _lopsided_rig(slope=0.09, down=0.7, law=None, **kw):
    """_rig() with a SimSlipStickZ whose step size varies with the true Z."""
    import camera.backends.sim as S
    orig = S.SimSlipStickZ.__init__

    def init(self, *a, **k):
        k["gain_slope"] = slope
        if law is not None:
            k["gain_law"] = law
        orig(self, *a, **k)
    S.SimSlipStickZ.__init__ = init
    try:
        return _rig(down=down, **kw)
    finally:
        S.SimSlipStickZ.__init__ = orig


def test_a_lopsided_walk_the_width_method_recovers_the_ratio():
    brain, z, events = _lopsided_rig()
    try:
        s = _run_zcal(brain)
        assert s.zcal_state == "OK", s.zcal_state
        want = 1.0 / 0.7
        assert s.zcal_ratio == pytest.approx(want, rel=0.03), (s.zcal_ratio, s.zcal_levels)
        # the walk IS lopsided: the old gate (0.97) would have refused it
        assert min(s.zcal_r2_up, s.zcal_r2_down) < 0.97, (s.zcal_r2_up, s.zcal_r2_down)
        assert math.isfinite(s.zcal_ratio_fit)
        assert s.zcal_ratio_spread < 0.10
        assert s.zcal_levels.count("x ") >= 3, s.zcal_levels
        up, dn = z.step_sizes()
        assert up / dn == pytest.approx(s.zcal_ratio, rel=1e-9)
        assert math.sqrt(up * dn) == pytest.approx(1.0, rel=1e-9)   # geometric mean kept
        msg = [m for _l, m in events if "ratio up/down" in m][-1]
        assert "WIDTHS" in msg and "diagnostic" in msg
        assert abs(_true(z) - ZF) < 0.6, _true(z) - ZF
    finally:
        brain.shutdown()


def test_a_lopsided_walk_the_old_gate_refuses_it():
    """zcal_fit_min_r2 = 0.97 is the old rule: it refuses the lopsided walk,
    as it did on the rig -- the reason the gate is now 0.8."""
    brain, z, events = _lopsided_rig()
    try:
        brain.cfg.autofocus.zcal_fit_min_r2 = 0.97
        sizes = z.step_sizes()
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed: poor fit"), s.zcal_state
        assert z.step_sizes() == sizes
    finally:
        brain.shutdown()


def test_a_direction_dependent_nonlinearity_is_refused():
    """The down steps grow away from focus, the up steps do not: no single
    ratio describes that Z. The per-level ratios disagree -> refused, nothing
    written -- although both parabolas fit well (the old method accepted it)."""
    brain, z, events = _lopsided_rig(
        slope=0.0, law=lambda zz, d: 1.0 if d > 0 else 1.0 + 0.8 * min(1.0, ((zz - ZF) / 4.0) ** 2))
    try:
        sizes = z.step_sizes()
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed: levels disagree"), s.zcal_state
        assert z.step_sizes() == sizes
        assert math.isnan(s.zcal_ratio)
        assert s.zcal_levels                         # the evidence is shown
        assert s.zcal_ratio_spread > 0.10
        assert min(s.zcal_r2_up, s.zcal_r2_down) > 0.97   # the old method was fooled
        assert any("per level" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_levels_that_are_not_reached_are_refused():
    brain, z, events = _rig()
    try:
        brain.cfg.autofocus.zcal_width_levels = "3.0, 3.5"     # above zcal_walk_to 2.5
        sizes = z.step_sizes()
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed: too few levels"), s.zcal_state
        assert z.step_sizes() == sizes
        assert any("not reached" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_a_first_walk_started_near_focus_is_walked_again_and_every_level_is_used():
    brain, z, events = _rig()
    try:
        brain.cfg.autofocus.zcal_start_offset_v = 0.5     # starts almost AT focus
        s = _run_zcal(brain)
        assert s.zcal_state == "OK", s.zcal_state
        assert any("walking up once more" in m for _l, m in events)
        assert s.zcal_levels.count("x ") == 4, s.zcal_levels   # 1.3, 1.5, 1.8, 2.0
        assert s.zcal_ratio == pytest.approx(1.0 / 0.7, rel=0.03)
        assert abs(_true(z) - ZF) < 0.6, _true(z) - ZF    # parked after an UP walk
        c = brain.get_zcal_curve()
        assert len(c["levels"]) == 4
        assert c["skip_first"] == 1
    finally:
        brain.shutdown()


def test_new_settings_travel_and_the_old_r2_gate_is_not_read_back(tmp_path):
    from camera.net.protocol import apply_config_dict, config_to_dict
    cfg = Config()
    af = cfg.autofocus
    af.zcal_width_levels, af.zcal_width_max_spread = "1.4, 1.9", 0.07
    af.zcal_width_min_levels, af.zcal_walk_to, af.zcal_skip_first = 3, 2.7, 2
    af.zcal_fit_min_r2 = 0.85
    wire = Config()
    apply_config_dict(wire, config_to_dict(cfg))
    w = wire.autofocus
    assert (w.zcal_width_levels, w.zcal_width_max_spread, w.zcal_width_min_levels,
            w.zcal_walk_to, w.zcal_skip_first, w.zcal_fit_min_r2) == (
        "1.4, 1.9", 0.07, 3, 2.7, 2, 0.85)
    p = tmp_path / "camera.ini"
    save_config(cfg, str(p))
    assert load_config(str(p)).autofocus.zcal_width_levels == "1.4, 1.9"
    # a camera.ini saved before 2026-09-29 holds zcal_min_r2 = 0.97 -- the gate
    # that refused the rig's walk. It must NOT come back under the new name.
    text = p.read_text(encoding="utf-8").replace("zcal_fit_min_r2 = 0.85", "zcal_min_r2 = 0.97")
    p.write_text(text, encoding="utf-8")
    assert load_config(str(p)).autofocus.zcal_fit_min_r2 == Config().autofocus.zcal_fit_min_r2
    assert Config().autofocus.zcal_fit_min_r2 < 0.955     # the rig's lopsided walk passes


def test_the_autofocus_tab_shows_levels_crossings_and_the_diagnostic():
    import os
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    brain, z, events = _rig()
    try:
        win = MainWindow(brain, brain.cfg, remote=False)
        assert "zcal_width_levels" in win._form_widgets["autofocus"]
        win.b_zcal.click()
        assert _wait(lambda: brain.status().zcal_id == 1 and not brain.status().zcal_running)
        win._refresh()
        text = win.lab_zcal.text()
        assert "width method" in text and "per σ² level" in text and "diagnostic" in text
        win._update_zcal_plot()
        c = brain.get_zcal_curve()
        n = len(c["levels"])
        assert n >= 2
        assert len(win.af_plot._hlines) == n
        assert len(win.af_plot._marks) == 4 * n      # two crossings per walk per level
        lev = c["levels"][0]
        assert (lev["up"][0], lev["level"], win.af_plot._marks[0][2]) == (
            win.af_plot._marks[0][0], win.af_plot._marks[0][1], win.af_plot._marks[0][2])
        assert not win.grab().isNull()
        # a refusal shows its evidence too
        brain.cfg.autofocus.zcal_width_max_spread = 0.0
        win.b_zcal.click()
        assert _wait(lambda: brain.status().zcal_id == 2 and not brain.status().zcal_running)
        win._refresh()
        assert "levels disagree" in win.lab_zcal.text()
        assert "per σ² level" in win.lab_zcal.text()
        win.close()
    finally:
        brain.shutdown()
        app.processEvents()
