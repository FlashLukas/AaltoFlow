"""The simulator's physics: grid, lines, dark scaling, saturation."""

import numpy as np

from ccs200 import model
from ccs200.config import Sim


def test_wavelength_grid_spans_200_to_1000_nm_monotonically():
    wl = model.pixel_wavelengths()
    assert wl.size == 3648
    assert abs(wl[0] - 200) < 1 and abs(wl[-1] - 1000) < 1
    d = np.diff(wl)
    assert (d > 0).all()
    assert d.max() / d.min() > 1.03          # nonlinear dispersion (~5 %), not a linspace


def test_lines_sit_at_their_tabulated_wavelengths():
    wl = model.pixel_wavelengths()
    sim = Sim(lamp_level_per_s=0.0)
    y = model.light(wl, sim, 0.01)
    assert abs(wl[np.argmax(y)] - 546.07) < 0.25
    assert abs(y.max() - 0.6) < 0.05         # the brightest line at 10 ms, as documented


def test_signal_scales_with_integration_and_saturates():
    wl = model.pixel_wavelengths()
    sim = Sim(read_noise=0.0)
    pat = model.dark_pattern()
    rng = np.random.default_rng(0)
    a = model.light(wl, sim, 0.005).max()
    b = model.light(wl, sim, 0.010).max()
    assert abs(b / a - 2.0) < 1e-9
    s = model.scan(wl, sim, 0.1, pat, rng)
    assert s.max() == 1.0 and s.min() >= 0.0       # clipped at full scale


def test_dark_grows_with_integration_time_and_has_a_fixed_pattern():
    sim = Sim()
    pat = model.dark_pattern()
    d1 = model.dark(sim, 1.0, pat)
    d2 = model.dark(sim, 2.0, pat)
    assert np.allclose(d2 - sim.offset, 2 * (d1 - sim.offset))
    assert np.array_equal(pat, model.dark_pattern())           # the same detector every time
    assert pat.std() > 0.1


def test_light_off_is_dark_only():
    wl = model.pixel_wavelengths()
    sim = Sim(light_on=False)
    assert not model.light(wl, sim, 1.0).any()
