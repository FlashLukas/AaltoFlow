"""Array-valued and complex detectors -- the VNA case.

A VNA does not return a number per scan point. It returns a whole trace,
because the frequency sweep happens IN HARDWARE, on the instrument, far faster
than the odometer could step it. That frequency axis is a genuine dimension of
the measurement; it is simply swept by the instrument rather than by the engine.

So a scan of N field points against a 401-point VNA trace is a (N, 401) cube,
and the two axes are equally real -- one software-swept, one hardware-swept.
"""

from __future__ import annotations

import numpy as np
import pytest

from scan_core.data import as_complex, complex_names, load, to_complex_dataset
from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import (AcquireSpec, AxisSpec, Gettable, Registry,
                              Settable, build_sim_registry)


def _scalar_registry():
    """A tiny registry with one settable and one scalar detector."""
    state = {"x": 0.0}
    reg = Registry()
    reg.add(Settable("x", "X", "mm", (-10, 10),
                     set_fn=lambda v: state.__setitem__("x", v),
                     get_fn=lambda: state["x"]))
    return reg, state


def test_array_detector_adds_its_own_dimension():
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 7}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")

    assert dict(ds.sizes) == {"field": 7, "vna_freq": 401}
    assert ds["s21_real"].dims == ("field", "vna_freq")
    assert ds.coords["vna_freq"].attrs["units"] == "Hz"


def test_scalar_and_array_detectors_coexist():
    """The scalar keeps the scan's shape; the array gets the extra dimension."""
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 60, "num": 5}],
               detectors=["s21", "lockin_r"])
    ds = run(r, reg, created_iso="t")

    assert ds["lockin_r"].dims == ("field",)
    assert ds["s21_real"].dims == ("field", "vna_freq")


def test_array_detector_under_a_2d_scan_gives_a_3d_cube():
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "rf_power",
                      "start": -20, "stop": 0, "num": 3},
                     {"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 4}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")
    assert ds["s21_real"].shape == (3, 4, 401)


def test_complex_is_split_so_the_file_stays_conforming_netcdf(tmp_path):
    """h5netcdf WILL write complex, but warns the file is not standard netCDF-4
    and "might not be readable by other netcdf tools". Lab data gets opened in
    MATLAB and Igor too, so the pair is the default."""
    import warnings

    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 30, "num": 3}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")

    assert "s21" not in ds.data_vars
    assert {"s21_real", "s21_imag"} <= set(ds.data_vars)
    assert complex_names(ds) == ["s21"]
    assert not np.iscomplexobj(ds["s21_real"].values)

    path = tmp_path / "fmr.nc"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ds.to_netcdf(path, engine="h5netcdf")
    offending = [str(w.message) for w in caught
                 if "invalid netcdf" in str(w.message).lower()]
    assert not offending, f"wrote a non-conforming netCDF file: {offending}"

    back = load(path)
    try:
        assert np.allclose(as_complex(back, "s21").values,
                           as_complex(ds, "s21").values)
    finally:
        back.close()


def test_as_complex_round_trips_and_reports_what_it_has():
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 30, "num": 3}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")

    s21 = as_complex(ds, "s21")
    assert s21.dtype == np.complex128
    assert s21.dims == ("field", "vna_freq")
    assert np.allclose(s21.real.values, ds["s21_real"].values)

    merged = to_complex_dataset(ds)
    assert "s21" in merged.data_vars and "s21_real" not in merged.data_vars

    # the usual mistake is the wrong case or a typo; say what IS there
    with pytest.raises(KeyError) as exc:
        as_complex(ds, "S21")
    assert "s21" in str(exc.value)


def test_the_resonance_moves_with_field():
    """Not a plumbing test: if the trace did not respond to the swept axis, the
    array plumbing could be perfectly correct and still be recording nothing."""
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 5}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")

    mag = np.abs(as_complex(ds, "s21").values)
    freqs = ds.coords["vna_freq"].values
    resonances = [freqs[row.argmin()] for row in mag]
    assert resonances == sorted(resonances), "resonance should rise with field"
    assert resonances[-1] > resonances[0] * 1.5


def test_ragged_data_stops_the_scan_instead_of_being_padded():
    """If the instrument's sweep changes mid-scan there is nowhere sensible to
    put the data. Padding would hand back a file that looks fine and is wrong."""
    reg, _ = _scalar_registry()
    n = {"v": 51}

    def shrinking():
        out = np.ones(n["v"])
        n["v"] = 33                       # someone reconfigures the VNA
        return out

    reg.add(Gettable("trace", "Trace", "", shrinking,
                     axes=[AxisSpec("f", "F", "Hz",
                                    values_fn=lambda: np.arange(51.0))]))
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "x",
                      "start": 0, "stop": 2, "num": 3}],
               detectors=["trace"])

    with pytest.raises(ValueError) as exc:
        run(r, reg, created_iso="t")
    msg = str(exc.value)
    assert "trace" in msg and "(33,)" in msg and "(51,)" in msg
    assert "mid-scan" in msg


def test_two_detectors_sharing_an_axis_share_one_coordinate():
    """s11 and s21 come off the same sweep; they must not get two axes."""
    reg, _ = _scalar_registry()
    freqs = np.linspace(1e9, 2e9, 9)

    def axis():
        return AxisSpec("vna_freq", "Frequency", "Hz", values_fn=lambda: freqs)

    reg.add(Gettable("s11", "S11", "", lambda: np.zeros(9), axes=[axis()]))
    reg.add(Gettable("s21", "S21", "", lambda: np.ones(9), axes=[axis()]))
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "x",
                      "start": 0, "stop": 1, "num": 2}],
               detectors=["s11", "s21"])
    ds = run(r, reg, created_iso="t")

    assert dict(ds.sizes) == {"x": 2, "vna_freq": 9}
    assert ds["s11"].dims == ds["s21"].dims == ("x", "vna_freq")


def test_detectors_disagreeing_about_a_shared_axis_is_refused():
    reg, _ = _scalar_registry()
    reg.add(Gettable("a", "A", "", lambda: np.zeros(9),
                     axes=[AxisSpec("f", "F", "Hz",
                                    values_fn=lambda: np.arange(9.0))]))
    reg.add(Gettable("b", "B", "", lambda: np.zeros(4),
                     axes=[AxisSpec("f", "F", "Hz",
                                    values_fn=lambda: np.arange(4.0))]))
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "x",
                      "start": 0, "stop": 1, "num": 2}],
               detectors=["a", "b"])

    with pytest.raises(ValueError) as exc:
        run(r, reg, created_iso="t")
    assert "disagree about axis 'f'" in str(exc.value)


def test_scalar_scans_are_untouched_by_any_of_this():
    """The whole point is that nothing about the existing path changes."""
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 6},
                     {"type": "linear", "param": "rf_freq",
                      "start": 500, "stop": 2500, "num": 9}],
               detectors=["lockin_r", "lockin_phi"])
    ds = run(r, reg, created_iso="t")

    assert dict(ds.sizes) == {"field": 6, "rf_freq": 9}
    assert ds["lockin_r"].dims == ("field", "rf_freq")
    assert ds["lockin_r"].dtype == float
    assert complex_names(ds) == []


def test_builder_offers_and_plots_magnitude_for_a_complex_detector():
    """The plotting path changed, so exercise it rather than assume it.

    An FMR map is read as |S21|; the real part on its own is not what anyone
    looks at, so the builder offers |x| and arg(x) ahead of the stored halves.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    try:
        win.add_axis("field")
        win.rows[0].start.setValue(0.0)
        win.rows[0].stop.setValue(120.0)
        win.rows[0].num.setValue(9)

        # tick S21 in the detector list
        from PySide6.QtCore import Qt
        for item in win._det_items():
            item.setCheckState(0, Qt.Checked if item.data(0, Qt.UserRole) == "s21"
                               else Qt.Unchecked)

        win.per_pt.setValue(0.0)
        win.run_scan(block=True)

        # A complex detector is ONE entry (the stored _real/_imag halves are an
        # implementation detail of the file), and which part you colour by is a
        # separate choice -- so the magnitude/phase question does not multiply
        # the detector list.
        names = [win.det_combo.itemText(i) for i in range(win.det_combo.count())]
        assert names[0] == "s21"
        assert "s21_real" not in names and "s21_imag" not in names
        assert win.view.part_combo.isVisibleTo(win.view), \
            "a complex detector must offer |z| / arg z / Re / Im"

        for part in ("|z|", "arg z", "Re z", "Im z"):
            win.view.part_combo.setCurrentText(part)
            win._update_plot()          # must not raise for any of them
    finally:
        win.close()


# --------------------------------------------------------------------------- #
# Acquisition: does the software actually WAIT for the instrument?
# --------------------------------------------------------------------------- #

def test_a_triggered_detector_records_the_current_point_not_the_previous_one():
    """The whole point of `acquire`.

    Reading a VNA cold returns whatever is in its buffer -- the previous sweep,
    taken at the previous field. Nothing raises. The map is one step behind and
    looks perfectly clean.
    """
    reg = build_sim_registry()
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 5}],
               detectors=["s21"])
    ds = run(r, reg, created_iso="t")

    mag = np.abs(as_complex(ds, "s21").values)
    freqs = ds.coords["vna_freq"].values
    measured = np.array([freqs[row.argmin()] for row in mag])

    # what the resonance SHOULD be at each field, computed independently
    state = reg._state
    expected = []
    for b in ds.coords["field"].values:
        state.field_mT = float(b)
        expected.append(state._f_res() * 1e6)
    expected = np.array(expected)

    df = np.abs(freqs[1] - freqs[0])
    assert np.all(np.abs(measured - expected) <= 3 * df), (
        "the trace does not match the field it was recorded at -- "
        f"measured {measured / 1e9} GHz, expected {expected / 1e9} GHz")


def test_without_a_trigger_the_data_is_one_step_behind():
    """Executable documentation for the trap `acquire` exists to avoid.

    Same scan, same detector, read cold. Every point gets the last sweep the
    instrument actually took, so the resonance never moves -- and nothing
    anywhere raises. The file is well formed. The data is meaningless.
    """
    reg = build_sim_registry()
    cold = reg.get("s21")
    reg.add(Gettable("s21_cold", "S21 cold", "", cold._get,
                     axes=cold.axes, dtype="complex"))     # NO acquire spec

    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 120, "num": 5}],
               detectors=["s21_cold"])
    ds = run(r, reg, created_iso="t")

    mag = np.abs(as_complex(ds, "s21_cold").values)
    freqs = ds.coords["vna_freq"].values
    measured = np.array([freqs[row.argmin()] for row in mag])

    state = reg._state
    expected = []
    for b in ds.coords["field"].values:
        state.field_mT = float(b)
        expected.append(state._f_res() * 1e6)
    expected = np.array(expected)

    # An untriggered VNA does not lag one step behind -- it keeps handing back
    # the last sweep it actually took. So every point carries the SAME trace,
    # the resonance does not move with field at all, and the scan still reports
    # success and writes a perfectly well-formed file.
    assert np.allclose(measured, measured[0]), \
        "the cold read refreshed itself; this test no longer proves anything"
    assert np.abs(measured[-1] - expected[-1]) > 20 * np.abs(freqs[1] - freqs[0]), \
        "the stale trace happened to match the last point"


def test_detectors_sharing_a_group_are_triggered_once_per_point():
    """s11/s21/s12/s22 come off ONE sweep. Four triggers would be four sweeps."""
    reg, _ = _scalar_registry()
    calls = {"trigger": 0, "wait": 0}
    shared = AcquireSpec("sweep",
                         trigger_fn=lambda: calls.__setitem__("trigger", calls["trigger"] + 1),
                         wait_fn=lambda: calls.__setitem__("wait", calls["wait"] + 1))
    axis = AxisSpec("f", "F", "Hz", values_fn=lambda: np.arange(4.0))
    for pid in ("s11", "s21", "s12", "s22"):
        reg.add(Gettable(pid, pid.upper(), "", lambda: np.zeros(4),
                         axes=[axis], acquire=shared))

    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "x",
                      "start": 0, "stop": 2, "num": 3}],
               detectors=["s11", "s21", "s12", "s22"])
    run(r, reg, created_iso="t")

    assert calls["trigger"] == 3, f"one sweep per point, got {calls['trigger']}"
    assert calls["wait"] == 3


def test_every_group_is_triggered_before_any_is_waited_on():
    """Two instruments should acquire concurrently, not one after the other."""
    reg, _ = _scalar_registry()
    order = []
    for name in ("vna", "lockin"):
        spec = AcquireSpec(name,
                           trigger_fn=lambda n=name: order.append(f"trigger:{n}"),
                           wait_fn=lambda n=name: order.append(f"wait:{n}"))
        reg.add(Gettable(f"{name}_out", name, "", lambda: 1.0, acquire=spec))

    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "x", "start": 0, "stop": 1, "num": 1}],
               detectors=["vna_out", "lockin_out"])
    run(r, reg, created_iso="t")

    assert order == ["trigger:vna", "trigger:lockin", "wait:vna", "wait:lockin"], \
        f"acquisitions were serialised instead of overlapped: {order}"


def test_detectors_without_an_acquire_spec_are_untouched():
    """A fast detector must not gain a trigger it does not need."""
    reg = build_sim_registry()
    assert reg.get("lockin_r").acquire is None
    r = Recipe(name="t",
               axes=[{"type": "linear", "param": "field",
                      "start": 0, "stop": 10, "num": 3}],
               detectors=["lockin_r"])
    ds = run(r, reg, created_iso="t")
    assert ds["lockin_r"].shape == (3,)
