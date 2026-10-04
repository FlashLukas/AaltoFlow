"""Data-type-aware storage (storage.py, 2026-10-04).

The storage type of every recorded quantity comes from the module's describe:
bool -> uint8, int -> the narrowest integer its min/max (or bits) allow, enum
-> an integer code with CF flag attributes, string -> variable-length text,
float -> float64 (float32 on request), complex -> two halves. In memory the
numbers stay float64 with NaN; the type is applied when the file is written.
A value that breaks its declared type stops the scan. Everything is
compressed. Old files still load.
"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")
xr = pytest.importorskip("xarray")

from scan_core import Recipe, build_sim_registry, run              # noqa: E402
from scan_core.manifest import register_manifest                   # noqa: E402
from scan_core.registry import Gettable                            # noqa: E402
from scan_core.storage import (COMPRESSION, Storage, StorageError,  # noqa: E402
                               choose_int)


# ───────────────────────────── choosing the type ─────────────────────────────

@pytest.mark.parametrize("lo, hi, dtype, fill", [
    (0, 100, "int8", -128),
    (0, 200, "uint8", 255),
    (0, 255, "int16", -32768),            # no spare value in uint8 -> wider
    (-128, 127, "int16", -32768),
    (-5, 30000, "int16", -32768),
    (0, 65534, "uint16", 65535),
    (-2**31 + 1, 2**31 - 1, "int32", -2**31),
    (-2**31, 2**31 - 1, "int64", -2**63),  # the whole int32 range: no spare
    (0, 2**32 - 2, "uint32", 2**32 - 1),
])
def test_the_narrowest_integer_with_a_spare_fill_value(lo, hi, dtype, fill):
    dt, fv = choose_int(lo, hi)
    assert (str(dt), fv) == (dtype, fill)
    assert not (lo <= fv <= hi)


@pytest.mark.parametrize("descriptor, kind, disk, fill", [
    ({"type": "bool"}, "bool", "uint8", 255),
    ({"type": "int", "min": 0, "max": 200}, "int", "uint8", 255),
    ({"type": "int"}, "int", "int32", -2**31),
    ({"type": "int", "bits": 12}, "int", "uint16", 65535),
    ({"type": "int", "bits": 16}, "int", "uint32", 2**32 - 1),
    ({"type": "int", "min": 0}, "int", "int32", -2**31),
    ({"type": "enum", "options": ["a", "b", "c"]}, "enum", "int8", -1),
    ({"type": "enum", "options": [str(i) for i in range(300)]}, "enum", "int16", -1),
    ({"type": "string"}, "string", "object", None),
    ({"type": "float"}, "float", "float64", None),
    ({"type": "float", "store": "float32"}, "float", "float32", None),
    ({"type": "float", "dtype": "complex"}, "complex", "float64", None),
    ({"type": "float", "dtype": "complex", "store": "float32"}, "complex", "float32", None),
    ({"type": "float", "dtype": "int", "min": -10, "max": 10}, "int", "int8", -128),
    ({}, "float", "float64", None),
])
def test_storage_from_a_descriptor(descriptor, kind, disk, fill):
    st = Storage.from_descriptor(descriptor)
    assert (st.kind, str(st.disk), st.fill) == (kind, disk, fill)


def test_a_control_readback_is_not_narrowed_by_its_setting_limits():
    d = {"type": "int", "min": 0, "max": 10}
    assert str(Storage.from_descriptor(d).disk) == "int8"
    assert str(Storage.from_descriptor(d, use_bounds=False).disk) == "int32"


def test_manifest_gives_every_parameter_its_storage():
    class Stub:
        name = "m"

        def status(self):
            return {"n": 3, "ok": True, "mode": "b", "txt": "hi", "p": 1.5, "i": 2}

        def command(self, verb, **kw):
            return {"ok": True}

    reg = build_sim_registry()
    register_manifest(reg, Stub(), {"module": "m", "parameters": [
        {"id": "n", "kind": "indicator", "type": "int", "min": 0, "max": 99,
         "read_path": ["n"]},
        {"id": "ok", "kind": "indicator", "type": "bool", "read_path": ["ok"]},
        {"id": "mode", "kind": "indicator", "type": "enum", "options": ["a", "b"],
         "read_path": ["mode"]},
        {"id": "txt", "kind": "indicator", "type": "string", "read_path": ["txt"]},
        {"id": "p", "kind": "indicator", "type": "float", "store": "float32",
         "read_path": ["p"]},
        {"id": "i", "kind": "control", "type": "int", "min": 0, "max": 5,
         "read_path": ["i"], "set": {"verb": "set_i", "arg": "i"}},
    ]}, prefix=True)
    got = {pid: (reg.get(pid).storage.kind, str(reg.get(pid).storage.disk))
           for pid in ("m.n", "m.ok", "m.mode", "m.txt", "m.p", "m.i")}
    assert got == {"m.n": ("int", "int8"), "m.ok": ("bool", "uint8"),
                   "m.mode": ("enum", "int8"), "m.txt": ("string", "object"),
                   "m.p": ("float", "float32"), "m.i": ("int", "int32")}
    assert reg.get("m.mode").dtype == "enum" and reg.get("m.txt").dtype == "string"


def _av_load(aaltoview_data, path):
    """aaltoview.data.load, read fully and closed (Windows keeps open files)."""
    with aaltoview_data.load(path) as ds:
        return ds.load()


# ─────────────────────────── a typed, part-measured scan ─────────────────────

def _typed_registry():
    """The simulator plus one detector per storage kind, each a known
    function of the field so the expected values can be computed."""
    reg = build_sim_registry()
    s = reg._state
    reg.add(Gettable("flag", "Flag", "", lambda: s.field_mT > 0,
                     storage=Storage("bool")))
    reg.add(Gettable("cnt", "Bounded count", "", lambda: int(s.field_mT) + 100,
                     storage=Storage("int", lo=0, hi=200)))
    reg.add(Gettable("big", "Unbounded int", "", lambda: int(s.field_mT) * 100000,
                     storage=Storage("int")))
    reg.add(Gettable("adc", "12-bit ADC", "counts", lambda: 4000 + int(s.field_mT),
                     storage=Storage("int", bits=12)))
    reg.add(Gettable("mode", "Mode", "",
                     lambda: "fast mode" if s.field_mT > 0 else "slow",
                     storage=Storage("enum", options=["slow", "fast mode", "off"])))
    reg.add(Gettable("note", "Note", "", lambda: f"B={s.field_mT:g}",
                     storage=Storage("string")))
    reg.add(Gettable("f64", "Float", "V", lambda: s.field_mT / 3))
    reg.add(Gettable("f32", "Float32", "V", lambda: s.field_mT / 3,
                     storage=Storage("float", store="float32")))
    return reg


DETS = ["flag", "cnt", "big", "adc", "mode", "note", "f64", "f32", "s21",
        "overload", "photon_counts", "lockin_state", "sample_region"]
FIELDS = np.linspace(-20, 20, 9)
N_DONE = 6                         # aborted after six of the nine points


def _aborted_scan(reg):
    r = Recipe(name="typed", axes=[{"type": "array", "param": "field",
                                    "values": FIELDS.tolist()}],
               detectors=list(DETS))
    seen = []

    def progress(done, total, eta):
        seen.append(done)
    return run(r, reg, on_progress=progress,
               should_abort=lambda: len(seen) >= N_DONE)


@pytest.fixture(scope="module")
def typed_file(tmp_path_factory):
    reg = _typed_registry()
    ds = _aborted_scan(reg)
    path = tmp_path_factory.mktemp("st") / "typed.nc"
    ds.to_netcdf(path)                       # the plain call every caller makes
    return ds, path


EXPECTED_DISK = {
    "flag": ("uint8", 255), "cnt": ("uint8", 255), "big": ("int32", -2**31),
    "adc": ("uint16", 65535), "mode": ("int8", -1), "f64": ("float64", None),
    "f32": ("float32", None), "s21_real": ("float64", None),
    "s21_imag": ("float64", None), "overload": ("uint8", 255),
    "photon_counts": ("uint16", 65535), "lockin_state": ("int8", -1),
}


def test_the_file_has_the_declared_types_on_disk(typed_file):
    _, path = typed_file
    with h5py.File(path, "r") as f:
        for name, (dtype, fill) in EXPECTED_DISK.items():
            d = f[name]
            assert str(d.dtype) == dtype, name
            fv = d.attrs.get("_FillValue")
            if fill is None:
                assert fv is None or np.isnan(fv[0]), name
            else:
                assert int(fv[0]) == fill, name
        for name in ("note", "sample_region"):
            assert h5py.check_string_dtype(f[name].dtype) is not None, name


def test_every_data_variable_is_compressed(typed_file):
    _, path = typed_file
    with h5py.File(path, "r") as f:
        for name in EXPECTED_DISK:
            assert f[name].compression == "gzip", name
            assert f[name].compression_opts == COMPRESSION["complevel"], name
            assert f[name].shuffle, name


def _expected(field):
    return {"flag": float(field > 0), "cnt": int(field) + 100,
            "big": int(field) * 100000, "adc": 4000 + int(field),
            "mode": 1.0 if field > 0 else 0.0, "note": f"B={field:g}",
            "f64": field / 3}


@pytest.mark.parametrize("reader", ["xarray", "aaltoview"])
def test_it_reads_back_to_the_measured_values_with_gaps_where_unmeasured(typed_file, reader):
    ds, path = typed_file
    if reader == "xarray":
        back = xr.load_dataset(path, engine="h5netcdf")
    else:
        aaltoview_data = pytest.importorskip("aaltoview.data")
        back = _av_load(aaltoview_data, path)
    for i, field in enumerate(FIELDS):
        exp = _expected(field)
        for name, value in exp.items():
            got = back[name].values[i]
            if i < N_DONE:
                if name == "note":
                    assert got == value
                else:
                    assert float(got) == pytest.approx(value), (name, i)
            elif name == "note":
                assert got == "", (name, i)
            else:
                assert np.isnan(got), (name, i)
        f32 = back["f32"].values[i]
        assert (np.isnan(f32) if i >= N_DONE
                else f32 == pytest.approx(field / 3, rel=1e-6))
    # the in-memory dataset and the file agree everywhere (NaN where NaN)
    for name in ("flag", "cnt", "big", "adc", "mode", "f64", "overload",
                 "photon_counts", "lockin_state", "s21_real"):
        np.testing.assert_allclose(back[name].values.astype(float),
                                   ds[name].values, equal_nan=True, err_msg=name)
    assert list(back["sample_region"].values) == list(ds["sample_region"].values)
    assert all(v == "" for v in back["sample_region"].values[N_DONE:])
    assert back.attrs["recipe_json"] == ds.attrs["recipe_json"]
    if reader == "aaltoview":
        s21 = aaltoview_data.as_complex(back, "s21")
        assert np.isfinite(s21.values[:N_DONE]).all()
        assert np.isnan(s21.values[N_DONE:]).all()


def test_in_memory_numbers_stay_float_with_nan(typed_file):
    ds, _ = typed_file
    for name in ("flag", "cnt", "big", "adc", "mode"):
        assert ds[name].dtype == np.float64
        assert np.isnan(ds[name].values[N_DONE:]).all()
    assert ds["note"].dtype == object


def test_enum_attributes_map_the_codes_back_to_the_options(typed_file):
    _, path = typed_file
    back = xr.load_dataset(path, engine="h5netcdf")
    a = back["mode"].attrs
    assert a["aaltoflow_type"] == "enum"
    assert list(a["flag_values"]) == [0, 1, 2]
    assert a["flag_meanings"].split() == ["slow", "fast_mode", "off"]
    options = json.loads(a["options_json"])
    assert options == ["slow", "fast mode", "off"]
    names = [options[int(c)] for c in back["mode"].values[:N_DONE]]
    assert names == ["fast mode" if f > 0 else "slow" for f in FIELDS[:N_DONE]]


def test_every_variable_says_its_declared_type(typed_file):
    _, path = typed_file
    back = xr.load_dataset(path, engine="h5netcdf")
    want = {"flag": "bool", "cnt": "int", "big": "int", "adc": "int",
            "mode": "enum", "note": "string", "f64": "float", "f32": "float",
            "s21_real": "complex", "s21_imag": "complex"}
    assert {k: back[k].attrs["aaltoflow_type"] for k in want} == want
    assert back["cnt"].attrs["declared_min"] == 0
    assert back["cnt"].attrs["declared_max"] == 200
    assert back["adc"].attrs["declared_bits"] == 12


# ───────────────────── a value that breaks its promise stops ─────────────────

@pytest.mark.parametrize("storage, value, needle", [
    (Storage("int", lo=0, hi=200), 201, "outside its declared range [0, 200]"),
    (Storage("int", lo=0, hi=200), -1, "outside its declared range"),
    (Storage("int", lo=0, hi=200), 2.5, "not a whole number"),
    (Storage("int", bits=12), 4096, "12 bits"),
    (Storage("int"), 2**31, "int32 (no min/max declared)"),
    (Storage("int"), "7", "not a number"),
    (Storage("bool"), 2, "bool"),
])
def test_a_value_that_breaks_its_declared_type_stops_the_scan(storage, value, needle):
    reg = build_sim_registry()
    s = reg._state
    # good at the first point, bad from the second on
    reg.add(Gettable("bad", "Bad", "",
                     lambda: value if s.field_mT > 0 else
                     (storage.options[0] if storage.kind == "enum" else 0),
                     storage=storage))
    r = Recipe(name="t", axes=[{"type": "array", "param": "field",
                                "values": [0, 5, 10]}],
               detectors=["lockin_r", "bad"])
    with pytest.raises(StorageError) as e:
        run(r, reg)
    assert needle in str(e.value)
    assert "detector 'bad' at grid index (1,)" in str(e.value)


def test_an_unknown_enum_value_is_not_measured_and_said_once():
    """Lukas, 2026-10-04: modules read back values outside their own option
    lists ("--", a front-panel time constant), so an unknown option must not
    stop a scan: the point is stored as not measured, and the log says it ONCE
    per detector and value."""
    reg = build_sim_registry()
    s = reg._state
    reg.add(Gettable("mode", "Mode", "",
                     lambda: "c" if s.field_mT > 0 else "a",
                     storage=Storage("enum", options=["a", "b"])))
    r = Recipe(name="t", axes=[{"type": "array", "param": "field",
                                "values": [0, 5, 10]}],
               detectors=["lockin_r", "mode"])
    logs = []
    ds = run(r, reg, on_log=logs.append)
    vals = ds["mode"].values
    assert vals[0] == 0 and np.isnan(vals[1]) and np.isnan(vals[2])
    said = [m for m in logs if "not one of its options" in m]
    assert len(said) == 1 and "'c'" in said[0] and "mode" in said[0]


def test_none_and_nan_are_not_measured_not_refused():
    st = Storage("int", lo=0, hi=10)
    assert np.isnan(st.to_memory(None)) and np.isnan(st.to_memory(float("nan")))
    assert Storage("string").to_memory(None) == ""
    assert np.isnan(Storage("enum", options=["a"]).to_memory(None))


def test_an_int_array_detector_is_checked_element_by_element():
    st = Storage("int", bits=8)
    np.testing.assert_array_equal(st.to_memory(np.array([0, 255, np.nan])),
                                  [0.0, 255.0, np.nan])
    with pytest.raises(StorageError):
        st.to_memory([0, 256])


# ──────────────────────────── size: what it saves ────────────────────────────

def test_a_mostly_integer_file_is_much_smaller(tmp_path, capsys):
    """100 x 100 points of bool / 12-bit / enum detectors plus one float."""
    reg = build_sim_registry()
    r = Recipe(name="ints", axes=[
        {"type": "linear", "param": "pos_y", "start": -40, "stop": 40, "num": 100},
        {"type": "linear", "param": "pos_x", "start": -40, "stop": 40, "num": 100}],
        fixed={"field": 40.0, "rf_freq": 890.0},
        detectors=["overload", "photon_counts", "lockin_state", "lockin_r"])
    ds = run(r, reg)
    new = tmp_path / "typed.nc"
    ds.to_netcdf(new)
    old = tmp_path / "float64.nc"
    _as_before(ds).to_netcdf(old)
    a, b = os.path.getsize(new), os.path.getsize(old)
    with h5py.File(new, "r") as f, h5py.File(old, "r") as g:
        per_var = {k: (f[k].id.get_storage_size(), g[k].id.get_storage_size())
                   for k in r.detectors}
    with capsys.disabled():
        print(f"\n[storage] 100x100 bool+12bit+enum+float: typed+compressed "
              f"{a} B vs uncompressed float64 {b} B ({a / b:.0%}); "
              f"per variable (new, old): {per_var}")
    assert a < 0.5 * b
    for k in ("overload", "photon_counts", "lockin_state"):
        assert per_var[k][0] < per_var[k][1] / 4, k


# ───────────────────────────────── fly scans ─────────────────────────────────

def test_fly_counts_are_unsigned_and_binned_means_are_floats(tmp_path):
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    # declare the streamed lock-in R an int: its binned MEAN is still a float
    reg.get("aux_in").storage = Storage("int")
    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
               axes=[{"type": "linear", "param": "pos_y", "start": -5, "stop": 5, "num": 2},
                     {"type": "fly", "param": "pos_x", "start": -10, "stop": 10,
                      "num": 21, "speed": 40, "speed_param": "stage_speed"}],
               detectors=["lockin_r", "aux_in"])
    ds = run(r, reg)
    path = tmp_path / "fly.nc"
    ds.to_netcdf(path)
    with h5py.File(path, "r") as f:
        assert str(f["lockin_r_n"].dtype) == "uint32"
        assert int(f["lockin_r_n"].attrs["_FillValue"][0]) == 2**32 - 1
        assert str(f["lockin_r_std"].dtype) == "float64"
        assert str(f["aux_in"].dtype) == "float64"       # a mean, not an int
        assert f["aux_in_n"].compression == "gzip"
    back = xr.load_dataset(path, engine="h5netcdf")
    np.testing.assert_array_equal(back["lockin_r_n"].values, ds["lockin_r_n"].values)
    assert back["aux_in"].attrs["declared_type"] == "int"
    assert back["aux_in"].attrs["aaltoflow_type"] == "float"


def test_a_fly_scan_refuses_enum_and_string_detectors():
    reg = build_sim_registry()
    reg.get("lockin_state").stream = reg.get("lockin_r").stream
    r = Recipe(name="fly", axes=[{"type": "fly", "param": "pos_x", "start": -10,
                                  "stop": 10, "num": 21, "speed": 40}],
               detectors=["lockin_state"])
    assert any("lockin_state" in e and "enum" in e for e in r.validate(reg))


# ─────────────────────── the resonance window still loads ────────────────────

def test_a_window_scan_still_writes_and_loads(tmp_path):
    reg = build_sim_registry()
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 30, "stop": 190,
                      "num": 5}],
               detectors=["fmr"],
               window={"detector": "fmr", "field": "field", "angle": "field_angle",
                       "model": "inplane", "params": {"g": 2.0, "meff_mT": 1750.0,
                                                      "hk_mT": 5.0},
                       "margin_MHz": 300.0, "full_every": 20, "track": True})
    ds = run(r, reg)
    path = tmp_path / "win.nc"
    ds.to_netcdf(path)
    with h5py.File(path, "r") as f:
        assert f["fmr"].compression == "gzip"
        assert f["fmr_measured"].compression == "gzip"
    aaltoview_data = pytest.importorskip("aaltoview.data")
    back = _av_load(aaltoview_data, path)
    assert back["fmr_measured"].dtype == bool
    np.testing.assert_array_equal(back["fmr_measured"].values, ds["fmr_measured"].values)
    np.testing.assert_allclose(back["fmr"].values, ds["fmr"].values)


# ───────────────────────── backward compatibility ────────────────────────────

def _as_before(ds):
    """The dataset as the engine wrote it before 2026-10-04: same variables,
    no encoding (float64, uncompressed), no type attributes."""
    old = ds.copy(deep=True)
    for v in old.variables.values():
        v.encoding = {}
        for a in ("aaltoflow_type", "declared_type", "declared_min",
                  "declared_max", "declared_bits", "flag_values",
                  "flag_meanings", "options_json"):
            v.attrs.pop(a, None)
    return old


def test_an_old_format_file_still_loads(tmp_path):
    reg = build_sim_registry()
    r = Recipe(name="old", axes=[{"type": "linear", "param": "field", "start": 0,
                                  "stop": 50, "num": 4}],
               detectors=["lockin_r", "s21"])
    ds = run(r, reg)
    path = tmp_path / "old.nc"
    _as_before(ds).to_netcdf(path)
    with h5py.File(path, "r") as f:
        assert f["lockin_r"].compression is None        # really the old format
    aaltoview_data = pytest.importorskip("aaltoview.data")
    back = _av_load(aaltoview_data, path)
    np.testing.assert_allclose(back["lockin_r"].values, ds["lockin_r"].values)
    np.testing.assert_allclose(aaltoview_data.as_complex(back, "s21").values,
                               ds["s21_real"].values + 1j * ds["s21_imag"].values)


def test_a_float_only_scan_writes_the_same_variables_and_values(tmp_path):
    reg = build_sim_registry()
    r = Recipe(name="f", axes=[{"type": "linear", "param": "field", "start": 0,
                                "stop": 50, "num": 6}],
               detectors=["lockin_r", "lockin_x", "s21"])
    ds = run(r, reg)
    new, old = tmp_path / "new.nc", tmp_path / "old.nc"
    ds.to_netcdf(new)
    _as_before(ds).to_netcdf(old)
    a = xr.load_dataset(new, engine="h5netcdf")
    b = xr.load_dataset(old, engine="h5netcdf")
    assert set(a.variables) == set(b.variables)
    for name in b.variables:
        assert a[name].dtype == b[name].dtype, name
        np.testing.assert_array_equal(a[name].values, b[name].values)
    assert a.attrs == b.attrs


# ───────────────────────── the Measurement tab's viewer ──────────────────────

def test_the_result_view_shows_typed_detectors_and_hides_text():
    """A text variable has nothing to plot; it must not reach the plot (and
    crash it), while bool / int / enum codes are drawn like any number."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ds = _aborted_scan(_typed_registry())
    v = DataView()
    v.set_dataset(ds)
    names = [v.det_combo.itemText(i) for i in range(v.det_combo.count())]
    assert "note" not in names and "sample_region" not in names
    for name in ("flag", "cnt", "mode", "lockin_state"):
        assert name in names
        v.det_combo.setCurrentText(name)
        v.refresh()
