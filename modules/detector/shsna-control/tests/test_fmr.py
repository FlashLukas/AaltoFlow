"""The simulated magnetic film (sim.fmr_on, 2026-09-28): Kittel in-plane with a
uniaxial anisotropy and out-of-plane, the line in a simulated sweep moving with
the field exactly as Kittel says, no line below saturation out of plane, a
windowed acquisition catching the predicted line (the whole FMR-window scan
with no hardware), and the field read from a FAKE magnet's status stream.

Scratch ports 18080-18089 for the fake magnets."""

import json
import math
import threading
import time

import numpy as np
import pytest
import zmq

from shsna import physics
from shsna.config import Config, Sim
from shsna.net.describe import build_manifest
from shsna.net.protocol import status_to_dict
from shsna.sim_system import build_sim_system

K = physics.gamma_Hz_per_mT(2.0)          # Hz per mT for g = 2


def _film(**kw) -> Sim:
    s = Sim(fmr_on=True, fmr_meff_mT=175.0, fmr_g=2.0, fmr_hk_mT=0.0,
            fmr_easy_axis_deg=0.0, fmr_alpha=0.003, fmr_linewidth_Hz=0.0,
            fmr_depth_dB=3.0, fmr_geometry="inplane")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---- the physics --------------------------------------------------------------------

def test_gamma_is_g_times_mu_b_over_h():
    assert physics.gamma_Hz_per_mT(2.0) == pytest.approx(27.99249e6, rel=1e-6)


def test_isotropic_in_plane_kittel():
    s = _film()
    for b in (10.0, 50.0, 200.0):
        f, fwhm = physics.fmr_resonance(b, 0.0, s)
        assert f == pytest.approx(K * math.sqrt(b * (b + 175.0)), rel=1e-12)
        assert fwhm == pytest.approx(0.003 * K * (2 * b + 175.0), rel=1e-12)
    # no anisotropy: the angle does not matter; a negative 1-axis field is the same line
    assert physics.fmr_resonance(50.0, 73.0, s)[0] == pytest.approx(physics.fmr_resonance(50.0, 0.0, s)[0])
    assert physics.fmr_resonance(-50.0, 0.0, s)[0] == pytest.approx(physics.fmr_resonance(50.0, 0.0, s)[0])
    assert math.isnan(physics.fmr_resonance(0.0, 0.0, s)[0])       # no field, no stiffness
    assert physics.fmr_resonance(50.0, 0.0, _film(fmr_linewidth_Hz=5e6))[1] == 5e6


def test_uniaxial_anisotropy_moves_the_line_and_tilts_the_magnetisation():
    hk, m, b = 20.0, 175.0, 50.0
    s = _film(fmr_hk_mT=hk, fmr_easy_axis_deg=30.0)
    # along the easy axis the anisotropy ADDS to the field ...
    f_easy = physics.fmr_resonance(b, 30.0, s)[0]
    assert f_easy == pytest.approx(K * math.sqrt((b + hk) * (b + hk + m)), rel=1e-9)
    # ... along the hard axis (saturated, B > Hk) it subtracts from B1
    f_hard = physics.fmr_resonance(b, 120.0, s)[0]
    assert f_hard == pytest.approx(K * math.sqrt((b - hk) * (b + m)), rel=1e-9)
    assert f_easy > physics.fmr_resonance(b, 0.0, _film())[0] > f_hard
    # below Hk along the hard axis the magnetisation does NOT follow the
    # field: sin(theta) = B / Hk from the easy axis
    th = physics.inplane_equilibrium_deg(10.0, 120.0, hk, 30.0)
    assert math.sin(math.radians(th - 30.0)) == pytest.approx(10.0 / hk, abs=1e-9)
    # and at an arbitrary angle it is a stationary point of the energy
    th = math.radians(physics.inplane_equilibrium_deg(35.0, 75.0, hk, 30.0) - 30.0)
    phi = math.radians(75.0 - 30.0)
    assert 35.0 * math.sin(th - phi) + 0.5 * hk * math.sin(2 * th) == pytest.approx(0, abs=1e-9)


def test_out_of_plane_kittel_and_no_line_below_saturation():
    s = _film(fmr_geometry="outofplane")
    assert physics.fmr_resonance(300.0, 0.0, s)[0] == pytest.approx(K * (300.0 - 175.0))
    assert physics.fmr_resonance(300.0, 0.0, s)[1] == pytest.approx(2 * 0.003 * K * 125.0)
    for b in (0.0, 100.0, 175.0):
        f, _ = physics.fmr_resonance(b, 0.0, s)
        assert math.isnan(f)
        assert not physics.fmr_dip_dB(np.linspace(1e9, 4e9, 11), f, 1e6, 3.0).any()


def test_the_dip_is_a_lorentzian_of_the_given_depth_and_width():
    f = np.array([2.99e9, 3.0e9, 3.01e9])
    d = physics.fmr_dip_dB(f, 3.0e9, 20e6, 3.0)
    np.testing.assert_allclose(d, [1.5, 3.0, 1.5])            # half height at +-FWHM/2


def test_film_off_is_todays_chain():
    f = np.linspace(700e6, 1300e6, 101)
    s = Sim()
    assert s.fmr_on is False
    np.testing.assert_array_equal(physics.chain_dB(f, s), physics.chain_dB(f, s, 50.0, 0.0))


# ---- the film in simulated sweeps ------------------------------------------------------

def _sna(**sim):
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 1.0e9, 4.4e9, 1001
    cfg.field.source = "manual"
    cfg.field.manual_mT, cfg.field.manual_angle_deg = 50.0, 0.0
    cfg.sim.fmr_on = True
    cfg.sim.fmr_linewidth_Hz = 20e6            # a few bins wide on a 3.4 MHz grid
    for k, v in sim.items():
        setattr(cfg.sim, k, v)
    v, s = build_sim_system(cfg, realtime=False, seed=11)
    v.start(run=False)
    return v


def _finish(v):
    for _ in range(50):
        if not v.status().acquiring:
            break
        v.step()
    assert not v.status().acquiring and v.status().acq_error == ""


def _reference(v):
    v.set_sim("dut_inserted", False)            # the thru: no line, no film
    v.take_reference()
    _finish(v)
    v.set_sim("dut_inserted", True)


def _dip_Hz(v, window=None):
    v.acquire(window=window)
    _finish(v)
    t = v.get_trace("transmission")
    return float(t["freqs_Hz"][int(np.nanargmin(t["transmission"]))]), t


def test_the_line_moves_with_the_manual_field_as_kittel_predicts_in_plane():
    v = _sna()
    try:
        _reference(v)
        bin_Hz = v.frequencies()[1] - v.frequencies()[0]
        for b in (30.0, 50.0, 80.0):
            v.set_sim("manual_field_mT", b)
            want = K * math.sqrt(b * (b + 175.0))
            got, t = _dip_Hz(v)
            assert got == pytest.approx(want, abs=1.5 * bin_Hz), b
            # 3 dB deep (plus the line's insertion loss), flat away from it
            assert np.nanmin(t["transmission"]) == pytest.approx(-1.5 - 3.0, abs=0.4)
            st = v.status()
            assert st.sim_field_mT == b and st.sim_fres_Hz == pytest.approx(want)
            assert st.sim_field_source == "manual" and st.sim_field_ok is True
        # the angle matters once there is an anisotropy
        v.set_sim("fmr_hk_mT", 20.0)
        v.set_sim("manual_field_mT", 50.0)
        v.set_sim("manual_angle_deg", 90.0)                 # the hard axis
        got, _ = _dip_Hz(v)
        assert got == pytest.approx(K * math.sqrt(30.0 * 225.0), abs=1.5 * bin_Hz)
    finally:
        v.shutdown()


def test_out_of_plane_line_and_none_below_saturation():
    v = _sna(fmr_geometry="outofplane")
    try:
        _reference(v)
        bin_Hz = v.frequencies()[1] - v.frequencies()[0]
        v.set_sim("manual_field_mT", 300.0)
        got, _ = _dip_Hz(v)
        assert got == pytest.approx(K * 125.0, abs=1.5 * bin_Hz)
        v.set_sim("manual_field_mT", 150.0)                  # below Meff = 175 mT
        _got, t = _dip_Hz(v)
        # no line: the transmission is the waveguide's flat loss, within the noise
        assert np.nanmin(t["transmission"]) > -1.5 - 0.3
        assert math.isnan(v.status().sim_fres_Hz)
    finally:
        v.shutdown()


def test_a_window_around_the_predicted_line_catches_it():
    """What scan-core will do per field point: predict f_res, sweep a window
    of bins around it, find the line inside."""
    v = _sna()
    try:
        _reference(v)
        f = v.frequencies()
        for b in (40.0, 70.0):
            v.set_sim("manual_field_mT", b)
            fr = K * math.sqrt(b * (b + 175.0))
            i = int(round((fr - f[0]) / (f[1] - f[0])))
            got, t = _dip_Hz(v, window=[i - 20, i + 20])
            assert t["window"] == [i - 20, i + 20]
            assert np.isfinite(t["transmission"]).sum() == 41
            assert got == pytest.approx(fr, abs=1.5 * (f[1] - f[0]))
    finally:
        v.shutdown()


def test_describe_grows_the_film_group_only_while_the_film_is_on():
    v = _sna(fmr_on=False)
    try:
        ids = {p["id"] for p in build_manifest(v)["parameters"]}
        assert "fmr_on" in ids and "sim_field" not in ids
        r0 = build_manifest(v)["revision"]
        v.set_sim("fmr_on", True)
        params = {p["id"]: p for p in build_manifest(v)["parameters"]}
        assert {"sim_field", "sim_angle", "sim_fres", "sim_field_in_use"} <= set(params)
        assert build_manifest(v)["revision"] != r0
        st = status_to_dict(v.status())
        for p in params.values():                          # every read path resolves
            if p.get("read_path") and p["group"] == "Simulation":
                assert p["read_path"][0] in st, p["id"]
        assert st["sim_fres_Hz"] == pytest.approx(K * math.sqrt(50 * 225.0))
    finally:
        v.shutdown()


def test_set_sim_film_knobs_are_checked():
    v = _sna()
    try:
        v.set_sim("fmr_geometry", "out-of-plane")
        assert v.cfg.sim.fmr_geometry == "outofplane"
        with pytest.raises(ValueError):
            v.set_sim("fmr_geometry", "diagonal")
        with pytest.raises(ValueError):
            v.set_sim("field_source", "earth")
        v.set_sim("fmr_alpha", 5.0)                            # clamped, not refused
        assert v.cfg.sim.fmr_alpha == 0.5
        v.set_sim("manual_field_mT", 1e6)
        assert v.cfg.field.manual_mT == 5000.0
    finally:
        v.shutdown()


# ---- the field from a magnet's status stream -------------------------------------------

class FakeMagnet:
    """Publishes status frames like a magnet service (PUB only)."""

    def __init__(self, port, frame):
        self.frame = frame
        self._stop = threading.Event()
        self._pub = zmq.Context.instance().socket(zmq.PUB)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pub.bind(f"tcp://127.0.0.1:{port}")
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            self._pub.send_multipart([b"status", json.dumps(self.frame).encode("utf-8")])
            time.sleep(0.02)
        self._pub.close(0)

    def stop(self):
        self._stop.set()
        self._t.join(timeout=1.0)


def _wait_field(v, pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v.step()                       # health() keeps the source in step with the config
        st = v.status()
        if pred(st):
            return st
        time.sleep(0.02)
    return v.status()


@pytest.mark.parametrize("source,port,frame,field,angle", [
    ("mag2d", 18080, {"measured_bx_mT": 30.0, "measured_by_mT": 40.0}, 50.0, 53.130102354),
    ("mag2dcal", 18082, {"measured_bx_mT": 0.0, "measured_by_mT": -70.0}, 70.0, -90.0),
    ("clMag", 18084, {"measured_field_mT": -60.0}, -60.0, 0.0),
    ("ppms", 18086, {"measured_field_mT": 120.0}, 120.0, 0.0),
])
def test_the_film_follows_a_magnet_service(source, port, frame, field, angle):
    magnet = FakeMagnet(port, frame)
    cfg = Config()
    cfg.sim.fmr_on = True
    cfg.field.source = source
    setattr(cfg.field, f"{source}_pub_port", port)
    cfg.field.manual_mT = 11.0                                  # the fallback, before it is heard
    cfg.field.stale_s = 0.3            # (a change of it would rebuild the subscription)
    v, _ = build_sim_system(cfg, realtime=False)
    try:
        v.start(run=False)
        st = _wait_field(v, lambda s: s.sim_field_ok)
        assert st.sim_field_ok and st.sim_field_source == source
        assert st.sim_field_mT == pytest.approx(field) and st.sim_angle_deg == pytest.approx(angle)
        want = physics.fmr_resonance(field, angle, cfg.sim)[0]
        assert st.sim_fres_Hz == pytest.approx(want)
        # the magnet goes quiet: the film keeps the last field, and status says so
        magnet.stop()
        st = _wait_field(v, lambda s: not s.sim_field_ok)
        assert not st.sim_field_ok and "stale" in st.sim_field_source
        assert st.sim_field_mT == pytest.approx(field)
    finally:
        magnet.stop()
        v.shutdown()


def test_a_magnet_not_heard_falls_back_to_the_manual_field():
    cfg = Config()
    cfg.sim.fmr_on = True
    cfg.field.source, cfg.field.mag2d_pub_port = "mag2d", 18088      # nobody there
    cfg.field.manual_mT = 42.0
    v, _ = build_sim_system(cfg, realtime=False)
    try:
        v.start(run=False)
        v.step()
        st = v.status()
        assert st.sim_field_mT == 42.0 and st.sim_field_ok is False
        assert "not heard" in st.sim_field_source
        # switching the film off drops the subscription and blanks the status
        v.set_sim("fmr_on", False)
        v.step()
        st = v.status()
        assert math.isnan(st.sim_field_mT) and st.sim_field_source == ""
        assert v.backend._field is None
    finally:
        v.shutdown()
