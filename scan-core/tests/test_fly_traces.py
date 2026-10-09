"""Fly scans with a VNA: whole TRACES and single FREQUENCY POINTS (2026-10-09).

Lukas: "I want streaming on vna both the complete trace or individual
frequency points". A VNA streams every completed sweep as one sample: the whole
complex trace, time-stamped at the sweep's MIDDLE, and -- from the same sweeps
-- single frequency points as scalar channels, each stamped at the moment THAT
point was measured (t_start + (i + 0.5) dt). The fly engine bins whole traces
element by element (coherent complex mean), the points like any scalar.

What is proved here:
  * the binning of traces: coherent, element-wise, n per pixel, NaN elements;
  * the point channels' exact timing (a fake VNA whose every point records
    WHERE the stage was at its moment: the point channel lands on the pixel
    centre, element i of the whole trace is off by v * (t_i - t_middle));
  * a field-flown VNA map on the simulator shows the Kittel line where the
    stepped map does, for the trace and for two point channels;
  * the refusals: u without a reference, a sweep changed mid-scan, a trace
    with more than one dimension;
  * the file opens in the data viewer (aaltoview), unchanged.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.flyscan import bin_samples, validate_fly
from scan_core.registry import (AxisSpec, Gettable, Registry, Settable, StreamSpec,
                                build_sim_registry, normalize_chunk)
from scan_core.sim_stream import SimSweepStreamer
from timed_stream import TimedStream, Track

WHERE = {"pos_x": 30.0, "pos_y": 50.0}      # the bare film: one clean Kittel line


# ───────────────────────────── binning, pure ─────────────────────────────────

def test_traces_are_binned_element_by_element_and_coherently():
    edges = np.array([0.0, 1.0, 2.0])
    t_pos, pos = np.array([0.0, 10.0]), np.array([0.0, 2.0])     # 0.2 per second
    # four traces of 3 points: two in pixel 0 (t = 1, 4), two in pixel 1 (t = 6, 9)
    t = np.array([1.0, 4.0, 6.0, 9.0])
    rng = np.random.default_rng(1)
    phase = np.exp(1j * rng.uniform(0, 2 * np.pi, (4, 3)))
    vals = phase.copy()
    vals[2, 1] = np.nan + 1j * np.nan                             # one point missing
    m, n, s = bin_samples(t_pos, pos, t, vals, edges)
    assert m.shape == (2, 3) and s.shape == (2, 3) and n.tolist() == [2, 2]
    assert np.allclose(m[0], vals[:2].mean(axis=0))              # coherent mean
    assert np.allclose(m[1, [0, 2]], vals[2:, [0, 2]].mean(axis=0))
    assert np.isclose(m[1, 1], vals[3, 1])                       # the NaN left out
    # the spread: rms distance from the complex mean
    assert np.allclose(s[0], np.sqrt(np.mean(np.abs(vals[:2] - m[0]) ** 2, axis=0)))
    assert s[1, 1] == 0.0                                        # one sample
    # an empty pixel is NaN with n = 0, a reversed axis maps back
    m2, n2, _ = bin_samples(t_pos, pos, t[:2], vals[:2], edges[::-1])
    assert n2.tolist() == [0, 2] and np.all(np.isnan(m2[0]))


def test_complex_noise_averages_away_coherently():
    """|mean z| -> 0 for random phases; the mean of |z| would stay 1."""
    rng = np.random.default_rng(3)
    t = np.linspace(0.0, 1.0, 4000)
    z = np.exp(1j * rng.uniform(0, 2 * np.pi, t.size))
    m, n, s = bin_samples([0.0, 1.0], [0.0, 1.0], t, z, [0.0, 1.0])
    assert n[0] == 4000 and abs(m[0]) < 0.05 and np.isclose(s[0], 1.0, atol=0.01)


def test_a_chunk_with_traces_points_errors_and_settings_normalises():
    c = normalize_chunk({
        "t": [1.0, 2.0], "t_start": [0.5, 1.5], "t_end": [1.5, 2.5],
        "values": {"s": {"re": [[1, 2, None], [3, 4, 5]], "im": [[0, 0, 0], [1, 1, 1]]},
                   "p1": {"re": [1.0, 2.0], "im": [0.5, None]}},
        "t_ch": {"p1": [0.7, 1.7]},
        "errors": {"u": "u needs a reference"},
        "settings": {"points": 3}, "delay_s": {"s": 0, "p1": 0}})
    assert c["values"]["s"].shape == (2, 3) and np.iscomplexobj(c["values"]["s"])
    assert np.isnan(c["values"]["s"][0, 2])
    assert np.isnan(c["values"]["p1"][1].imag)
    assert c["t_ch"]["p1"].tolist() == [0.7, 1.7]
    assert c["errors"] == {"u": "u needs a reference"}
    assert c["settings"] == {"points": 3} and c["t_end"].tolist() == [1.5, 2.5]
    with pytest.raises(ValueError):                      # p1 has its own 2 stamps
        normalize_chunk({"t": [1.0], "values": {"p1": [1.0, 2.0]},
                         "t_ch": {"p1": [1.0]}})


def test_the_settings_pin_refuses_a_change_within_a_scan():
    chunks = iter([{"t": [], "values": {}, "settings": {"points": 3}},
                   {"t": [], "values": {}, "settings": {"points": 3}},
                   {"t": [], "values": {}, "settings": {"points": 5}}])
    spec = StreamSpec("vna.trace", lambda: None, lambda: next(chunks))
    spec.pin_reset()
    spec.read()
    spec.read()
    with pytest.raises(ValueError, match="settings changed"):
        spec.read()


# ─────────────────────────── the point timing ────────────────────────────────

def test_sim_sweeps_stamp_every_point_at_its_own_moment():
    f = np.linspace(1e9, 2e9, 11)
    s = SimSweepStreamer("x", lambda: f, lambda ff, fields: fields + 0j,
                         lambda: 0.0, lambda: 0.03, quantities={"s": lambda z: z},
                         points=lambda: {"p1": 0, "p2": 7})
    s.start()
    time.sleep(0.2)
    c = s.stop()
    t0, t1 = np.asarray(c["t_start"]), np.asarray(c["t_end"])
    assert len(t0) >= 2 and np.all(t1 - t0 >= 0.029)
    assert np.allclose(c["t"], 0.5 * (t0 + t1))                  # the trace: its middle
    assert np.allclose(c["t_ch"]["p1"], t0 + 0.5 / 11 * (t1 - t0))
    assert np.allclose(c["t_ch"]["p2"], t0 + 7.5 / 11 * (t1 - t0))
    assert c["values"]["s"].shape == (len(t0), 11)
    assert c["settings"]["channels"] == {"p1": 0, "p2": 7}


class _TimedVna:
    """A VNA on its OWN clock (no thread to be late, timed_stream.py): sweep k
    runs from t_go + k T to t_go + (k + 1) T, and point i of it records WHERE
    THE STAGE WAS at its moment t_i = t_start + (i + 0.5) T / n -- so a
    binned value IS the position it was binned at, if the time stamp is right."""

    def __init__(self, track, n=21, sweep_s=0.25, points=(0, 10, 20)):
        self.track, self.n, self.T, self.points = track, n, sweep_s, points
        self.lock = threading.Lock()
        self.next = None

    def start(self):
        with self.lock:
            self.next = time.time()

    def _take(self):
        rows = []
        if self.next is not None:
            while self.next + self.T <= time.time():
                rows.append(self.next)
                self.next += self.T
        n, T = self.n, self.T
        t0 = np.array(rows)
        ti = t0[:, None] + (np.arange(n) + 0.5) / n * T if rows else np.zeros((0, n))
        z = np.array([[self.track.pos(t) for t in row] for row in ti]) + 0j \
            if rows else np.zeros((0, n), complex)
        return {"t": (t0 + T / 2).tolist(), "t_start": t0.tolist(),
                "t_end": (t0 + T).tolist(),
                "values": {"s": {"re": z.real.tolist(), "im": z.imag.tolist()},
                           **{f"p{i}": {"re": z[:, i].real.tolist(),
                                        "im": z[:, i].imag.tolist()}
                              for i in self.points}},
                "t_ch": {f"p{i}": ti[:, i].tolist() for i in self.points},
                "delay_s": {}, "settings": {"points": n}}

    def read(self):
        with self.lock:
            return self._take()

    def stop(self):
        with self.lock:
            c = self._take()
            self.next = None
            return c


def _timing_rig(sweep_s=0.25, speed=8.0):
    track = Track()
    state = {"speed": speed}

    def move(v, timeout_s=None):
        t1 = track.move(v, state["speed"])
        while time.time() < t1:
            time.sleep(0.002)

    reg = Registry()
    reg.add(Settable("x", "X", "um", (-100, 100), move, lambda: track.pos()))
    reg.add(Settable("speed", "Speed", "um/s", (0.1, 100),
                     lambda v: state.__setitem__("speed", v), lambda: state["speed"]))
    pos = TimedStream(lambda t: {"x": track.pos(t)}, 500.0)
    reg.get("x").stream = StreamSpec("stage", pos.start, pos.read, pos.stop)
    reg.get("x").stream_channel = "x"
    vna = _TimedVna(track, sweep_s=sweep_s)
    spec = StreamSpec("vna", vna.start, vna.read, vna.stop)
    axis = AxisSpec("freq", "Frequency", "Hz", values_fn=lambda: np.arange(vna.n))
    g = reg.add(Gettable("s", "S", "", lambda: np.zeros(vna.n, complex), axes=[axis],
                         dtype="complex"))
    g.stream, g.stream_channel = spec, "s"
    for i in vna.points:
        p = reg.add(Gettable(f"p{i}", f"S point {i}", "", lambda: 0j, dtype="complex"))
        p.stream, p.stream_channel = spec, f"p{i}"
    return reg, vna


def test_point_channels_are_binned_at_their_own_moment_the_trace_at_its_middle():
    # A SLOW sweep on purpose: 0.75 s at 8 um/s = 6 um (3 pixels) per sweep,
    # so a wrong time stamp moves a value by pixels, not by a fraction of one.
    speed, T = 8.0, 0.75
    reg, vna = _timing_rig(sweep_s=T, speed=speed)
    r = Recipe(name="timing", detectors=["s", "p0", "p10", "p20"],
               axes=[{"type": "fly", "param": "x", "start": 0.0, "stop": 40.0,
                      "num": 21, "speed": speed, "speed_param": "speed"}])
    assert validate_fly(r, reg) == []
    ds = run(r, reg)
    x = ds["x"].values
    for i in (0, 10, 20):
        p = ds[f"p{i}_real"].values
        has = np.isfinite(p)
        assert has.sum() >= 4, p
        # the point channel: each value IS the stage position at its own
        # moment, and was binned by the position at that moment -- so it lies
        # inside its pixel (half a pixel = 1 um either side of the centre)
        assert np.max(np.abs(p[has] - x[has])) <= 1.0 + 1e-6, (i, p - x)
        # element i of the WHOLE trace is filed at the sweep's middle, so it
        # lands v * (t_i - t_middle) away -- up to 2.9 um here, what a point
        # channel avoids
        # (inner pixels only: at the row's ends the stage accelerates or sits
        # still, and a constant v is what the shift assumes)
        e = ds["s_real"].values[:, i]
        shift = speed * ((i + 0.5) / vna.n - 0.5) * T
        ok = np.isfinite(e)
        ok[[0, -1]] = False
        assert np.max(np.abs(e[ok] - x[ok] - shift)) <= 1.0 + 1e-6, (i, e - x, shift)
    # n: sweeps per pixel, one number per pixel (not per frequency point)
    assert ds["s_n"].dims == ("x",) and ds["s_std"].dims == ("x", "freq")


# ───────────────────────── the simulator, field flown ────────────────────────

def _dip_rows(ds, det="s21"):
    """Frequency of the |S21| minimum per field pixel."""
    s = ds[f"{det}_real"].values + 1j * ds[f"{det}_imag"].values
    f = ds["vna_freq"].values
    return f[np.nanargmin(np.abs(s), axis=-1)]


def _field_fly(**kw):
    ax = {"type": "fly", "param": "field", "start": 30.0, "stop": 70.0, "num": 21,
          "speed": 10.0}
    ax.update(kw)
    return ax


def test_field_flown_vna_trace_shows_the_kittel_line_where_stepping_does():
    reg = build_sim_registry()
    st = reg._state
    r = Recipe(name="fmr_fly", fixed=WHERE, detectors=["s21"], axes=[_field_fly()])
    assert r.validate(reg) == []
    log = []
    ds = run(r, reg, on_log=log.append)
    assert ds["s21_real"].dims == ("field", "vna_freq")
    assert ds["s21_n"].dims == ("field",) and np.all(ds["s21_n"].values >= 3)
    assert ds["field"].attrs["fly_binned_by"] == "measurement"
    fly = _dip_rows(ds)
    bin_hz = float(np.diff(ds["vna_freq"].values[:2])[0])
    truth = np.array([float(st._f_res(b)) * 1e6 for b in ds["field"].values])
    assert np.all(np.abs(fly - truth) <= 2 * bin_hz), (fly - truth) / bin_hz
    step = run(Recipe(name="fmr_step", fixed=WHERE, detectors=["s21"],
                      axes=[{"type": "linear", "param": "field", "start": 30.0,
                             "stop": 70.0, "num": 21}]), reg)
    assert np.all(np.abs(fly - _dip_rows(step)) <= 2 * bin_hz)
    assert not reg.get("field")._sim_ramp.running


def test_two_point_channels_find_the_field_of_their_frequency():
    reg = build_sim_registry()
    st = reg._state
    dets = ["s21_pt1", "s21_pt2"]
    r = Recipe(name="pts", fixed=WHERE, detectors=dets, zigzag=True,
               axes=[{"type": "array", "param": "rf_power", "values": [-10.0, -10.0]},
                     _field_fly(start=20.0, stop=100.0, num=41, speed=16.0)])
    assert r.validate(reg) == []
    ds = run(r, reg)
    step = run(Recipe(name="pts_step", fixed=WHERE, detectors=dets,
                      axes=[{"type": "linear", "param": "field", "start": 20.0,
                             "stop": 100.0, "num": 41}]), reg)
    B = ds["field"].values
    fine = np.linspace(0, 200, 20001)
    for k, det in enumerate(dets, start=1):
        f_pt = float(reg.get(det).label.split(" at ")[1].split()[0]) * 1e9
        b_star = fine[np.argmin(np.abs(np.array([st._f_res(b) for b in fine]) * 1e6 - f_pt))]
        for row in range(2):                       # forward and (zig-zag) backward
            mag = np.abs(ds[f"{det}_real"].values[row] + 1j * ds[f"{det}_imag"].values[row])
            assert abs(B[np.nanargmin(mag)] - b_star) <= 4.0, (det, row, b_star)
        smag = np.abs(step[f"{det}_real"].values + 1j * step[f"{det}_imag"].values)
        assert abs(B[np.nanargmin(smag)] - b_star) <= 4.0
    assert ds["s21_pt1_n"].dims == ("rf_power", "field")


# ────────────────────────────── refusals ─────────────────────────────────────

def test_u_without_a_reference_stops_the_scan_with_the_reason():
    reg = build_sim_registry()
    r = Recipe(name="u", fixed=WHERE, detectors=["u"], axes=[_field_fly(num=5)])
    assert r.validate(reg) == []
    with pytest.raises(Exception, match="needs a reference"):
        run(r, reg)
    # with a reference it flies
    reg._state.field_mT = 180.0
    reg._state.take_vna_reference()           # what the vna_reference action does
    ds = run(r, reg)
    assert np.isfinite(ds["u_real"].values).any()


def test_a_sweep_changed_between_rows_is_refused_and_the_rows_kept():
    reg = build_sim_registry()
    st = reg._state
    r = Recipe(name="chg", fixed=WHERE, detectors=["s21"],
               axes=[{"type": "array", "param": "rf_power", "values": [-10.0, -5.0]},
                     _field_fly(num=5, start=40.0, stop=48.0)])

    def progress(done, total, eta, where=None):
        if done == 5:                              # the first row is in
            st._vna_freqs = np.linspace(600e6, 6.0e9, 401)
    with pytest.raises(Exception, match="settings changed") as err:
        run(r, reg, on_progress=progress)
    ds = getattr(err.value, "dataset", None)
    assert ds is not None and np.isfinite(ds["s21_real"].values[0]).any()


def test_what_a_fly_scan_says_about_traces():
    reg = build_sim_registry()
    errs = validate_fly(Recipe(name="t", detectors=["fmr"], axes=[_field_fly()]), reg)
    assert any("does not stream traces" in e and "vna" in e for e in errs), errs
    assert validate_fly(Recipe(name="t", detectors=["s21", "s21_pt1"],
                               axes=[_field_fly()]), reg) == []
    two_d = reg.add(Gettable("img", "Image", "", lambda: np.zeros((2, 2)),
                             axes=[AxisSpec("a", "a", ""), AxisSpec("b", "b", "")]))
    two_d.stream, two_d.stream_channel = reg.get("s21").stream, "img"
    errs = validate_fly(Recipe(name="t", detectors=["img"], axes=[_field_fly()]), reg)
    assert any("2-D" in e for e in errs), errs


# ────────────────────────────── the viewer ───────────────────────────────────

def test_the_data_viewer_opens_a_flown_trace_map(tmp_path):
    from scan_core.data import load
    from scan_core.view import detector, detector_names, reduce_cube
    reg = build_sim_registry()
    ds = run(Recipe(name="viewer", fixed=WHERE, detectors=["s21", "s21_pt1"],
                    axes=[_field_fly(num=9, start=40.0, stop=56.0, speed=16.0)]), reg)
    path = tmp_path / "fly_vna.nc"
    ds.to_netcdf(path)
    back = load(path)
    names = detector_names(back)
    for want in ("s21", "s21_pt1", "s21_n", "s21_std", "s21_pt1_std", "s21_pt1_n"):
        assert want in names, names
    da = detector(back, "s21")
    assert np.iscomplexobj(da.values) and da.dims == ("field", "vna_freq")
    img = reduce_cube(da, x="vna_freq", y="field", part="abs")
    assert img.data.shape == (9, 401)
    line = reduce_cube(detector(back, "s21_n"), x="field", y=None)
    assert line.data.shape == (9,)
    back.close()
