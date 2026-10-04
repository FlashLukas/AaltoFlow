"""Bugs found in the deep cleaning of 2026-09-28, each proved by a test that
failed before its fix.

  1. Abort pressed during a SETTLE WAIT (where a real scan spends its time)
     threw every measured point away; Abort between two points kept them.
  2. A checkpoint that could not be written told the GUI "the run is over":
     Run came back on, Abort went off, and the scan carried on unstoppable.
  3. A fly scan hardly ever checkpointed: its progress jumps by whole rows,
     and "done % every == 0" is only true by coincidence.
  4. The approach to a fly row ignored Abort for up to its whole timeout.
  5. Two axes with one dimension name ran the whole scan and then failed to
     build the dataset -- the data lost, the after-scan routine never run.
  6. A text detector (a state name) passed validation and crashed the scan at
     its first point.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from scan_core import Recipe, build_sim_registry, run          # noqa: E402
from scan_core.errors import ScanAborted                       # noqa: E402
from scan_core.manifest import register_manifest                # noqa: E402
from scan_core.registry import Registry, Settable              # noqa: E402


def _abort_on_set(reg, pid, at: int):
    """Make the `at`-th set of `pid` raise ScanAborted, as a settle wait does
    when the operator presses Abort while the instrument is still settling."""
    p = reg.get(pid)
    orig, n = p._set, {"k": 0}

    def set_fn(value):
        n["k"] += 1
        if n["k"] == at:
            raise ScanAborted(f"{pid}: aborted while waiting")
        orig(value)
    p._set = set_fn
    p._takes_timeout = False


def _recipe(num=6):
    return Recipe(name="t", axes=[{"type": "linear", "param": "field",
                                   "start": 0, "stop": 50, "num": num}],
                  detectors=["lockin_r"])


# ---- 1. abort in a settle wait ------------------------------------------------

def test_an_abort_during_a_settle_wait_keeps_the_measured_points():
    reg = build_sim_registry()
    _abort_on_set(reg, "field", at=4)          # points 1..3 measured, then Abort
    with pytest.raises(ScanAborted) as info:
        run(_recipe(), reg)
    ds = getattr(info.value, "dataset", None)
    assert ds is not None, "the measured points were thrown away"
    vals = ds["lockin_r"].values
    assert np.isfinite(vals[:3]).all() and np.isnan(vals[3:]).all()


def test_an_abort_before_the_first_point_carries_no_empty_dataset():
    """Aborted inside a condition or the before-scan routine: nothing measured,
    so nothing to save (no empty file per aborted magnet ramp)."""
    reg = build_sim_registry()
    _abort_on_set(reg, "rf_power", at=1)
    r = _recipe()
    r.fixed = {"rf_power": -5.0}
    with pytest.raises(ScanAborted) as info:
        run(r, reg)
    assert getattr(info.value, "dataset", None) is None


def test_the_worker_saves_what_was_measured_before_an_abort_in_a_wait(tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanWorker
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    reg = build_sim_registry()
    _abort_on_set(reg, "field", at=4)
    path = tmp_path / "run.nc"
    w = ScanWorker(_recipe(), reg, save_path=path)
    w.run()                                   # in this thread
    assert w.outcome == "aborted"
    assert path.is_file(), "nothing written for an abort during a settle wait"
    with xr.open_dataset(path) as ds:
        assert int(np.isfinite(ds["lockin_r"].values).sum()) == 3


# ---- 2. a failed checkpoint must not end the run in the GUI -------------------

def test_a_failed_save_is_not_reported_as_the_end_of_the_run(tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanWorker
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    w = ScanWorker(None, None, save_path=blocker / "sub" / "run.nc")   # cannot exist
    failed, save_failed = [], []
    w.failed.connect(failed.append)
    w.save_failed.connect(save_failed.append)
    w._write(xr.Dataset({"a": ("x", [1.0])}), 1, 10)
    assert failed == [], "`failed` means the run is over; the scan is still going"
    assert save_failed and "could not save" in save_failed[0]


def test_the_builder_keeps_control_of_a_scan_whose_checkpoint_failed(tmp_path):
    """End to end: a scan whose checkpoints cannot be written keeps its Abort
    button and its worker, and Abort still stops it."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    reg = build_sim_registry()
    gate = threading.Event()
    p = reg.get("field")
    orig, n = p._set, {"k": 0}

    def slow(value):                  # point 30 of 200 waits for the gate
        n["k"] += 1
        if n["k"] == 30:
            gate.wait(20)
        orig(value)
    p._set = slow

    blocker = tmp_path / "a_file"
    blocker.write_text("x")           # the "data directory" is a file: saving fails
    win = ScanBuilder(reg)
    win.ask_before_unsaved = False
    win.autosave_dir = blocker
    try:
        win.add_axis("field")
        win.rows[0].num.setValue(200)      # > 100 points: checkpoints every 20
        win.run_scan()
        worker = win.worker
        t_end = time.monotonic() + 10
        while "could not save" not in win.save_lbl.text() and time.monotonic() < t_end:
            app.processEvents(); time.sleep(0.01)
        assert "could not save" in win.save_lbl.text()
        assert win.worker is worker and worker.isRunning()
        assert win.abort_btn.isEnabled() and not win.run_btn.isEnabled()
        win._abort()
        gate.set()
        assert worker.wait(10000)
        app.processEvents()
        assert worker.outcome == "aborted"
    finally:
        gate.set()
        if win.worker is not None:
            win.worker.wait(10000)
        win.close()


# ---- 3. fly scans checkpoint too ---------------------------------------------

def test_checkpoints_follow_progress_that_jumps():
    """A fly scan reports progress a row at a time (and in pieces within it);
    the checkpoint must fire when a tenth has been PASSED, not only when the
    count lands on a multiple exactly."""
    pytest.importorskip("PySide6")
    from apps.scan_builder import ScanWorker

    w = ScanWorker(None, None, save_path=None)
    written = []
    w._write = lambda ds, done, total: written.append(done)
    total = 15 * 81                           # 15 rows of 81 pixels
    for done in range(81, total + 1, 81):
        w._live(done, total, lambda: None)
    assert len(written) >= 8, written          # ~every tenth, not never
    assert all(d < total for d in written)     # the final save covers the end

    # a stepped scan checkpoints exactly as before
    s = ScanWorker(None, None, save_path=None)
    got = []
    s._write = lambda ds, done, total: got.append(done)
    for done in range(1, 251):
        s._live(done, 250, lambda: None)
    assert got == [25, 50, 75, 100, 125, 150, 175, 200, 225]


# ---- 4. abort during a fly row's approach ------------------------------------

def test_abort_reaches_the_wait_for_a_fly_rows_run_in():
    """A stage whose move returns at once (stale "not moving" frame) and that
    then crawls towards the run-in: the approach waits on the MEASURED
    position -- and must still see Abort, not sit out the row timeout."""
    from scan_core.sim_stream import SimStreamer

    state = {"x": 100.0, "target": 100.0, "speed": 5.0, "stop": False}

    def mover():
        while not state["stop"]:
            d = state["target"] - state["x"]
            step = state["speed"] * 0.01
            state["x"] += max(-step, min(step, d))
            time.sleep(0.01)
    threading.Thread(target=mover, daemon=True).start()

    reg = Registry()
    x = reg.add(Settable("x", "X", "um", (-200, 200),
                         set_fn=lambda v: state.__setitem__("target", v),
                         get_fn=lambda: state["x"]))
    sig = reg.add(Settable("sig", "Signal", "V", (-1, 1),
                           set_fn=lambda v: None, get_fn=lambda: 0.0))
    stage = SimStreamer("s.stage", lambda: {"x": state["x"]}, rate_hz=100.0).spec()
    det = SimStreamer("s.det", lambda: {"v": 0.0}, rate_hz=100.0).spec()
    x.stream, x.stream_channel = stage, "x"
    sig.stream, sig.stream_channel = det, "v"

    r = Recipe(name="fly", axes=[{"type": "fly", "param": "x", "start": 0,
                                  "stop": 10, "num": 11, "speed": 5}],
               detectors=["sig"])
    assert r.validate(reg) == []
    t0 = time.monotonic()
    abort_at = t0 + 0.4
    try:
        with pytest.raises(ScanAborted):
            run(r, reg, should_abort=lambda: time.monotonic() > abort_at)
        assert time.monotonic() - t0 < 5.0, "Abort waited for the stage to arrive"
    finally:
        state["stop"] = True                  # end the pretend stage's thread


class _StubInst:
    """Enough of an Instrument for register_manifest, without a socket."""
    name = "stub"

    def __init__(self, status, replies=None):
        self._status, self._replies = status, replies or {}

    def status(self):
        return dict(self._status)

    def command(self, verb, **kw):
        return {"ok": True, **self._replies.get(verb, {})}


# ---- 5. two dims with one name are refused before anything moves -------------

def test_two_axes_with_one_dimension_name_are_refused():
    reg = build_sim_registry()
    r = Recipe(name="t", axes=[
        {"type": "linear", "param": "field", "start": 0, "stop": 10, "num": 2},
        {"type": "linear", "param": "rf_freq", "name": "field",
         "start": 900, "stop": 1000, "num": 3}], detectors=["lockin_r"])
    errs = r.validate(reg)
    assert any("field" in e and "name" in e for e in errs), errs


def test_the_same_parameter_on_two_axes_is_refused():
    reg = build_sim_registry()
    r = Recipe(name="t", axes=[
        {"type": "linear", "param": "field", "start": 0, "stop": 10, "num": 2},
        {"type": "linear", "param": "field", "start": 0, "stop": 5, "num": 3}],
        detectors=["lockin_r"])
    assert r.validate(reg), "validated, and would have failed only at the end"


# ---- 6. text detectors ---------------------------------------------------------

def test_typed_text_detectors_are_recorded_but_not_scanned():
    """Until 2026-10-04 a string/enum detector was refused ("text, not a
    number"). Since data-type-aware storage (storage.py) a TYPED one is
    recorded -- an enum as its option's code, a string as text -- and the
    scan runs; it is still refused as an axis (it is not a settable)."""
    inst = _StubInst({"state": "IDLE", "mode": "fast"})
    reg = build_sim_registry()
    register_manifest(reg, inst, {"module": "m", "parameters": [
        {"id": "state", "kind": "indicator", "type": "string", "read_path": ["state"]},
        {"id": "mode", "kind": "control", "type": "enum", "options": ["fast", "slow"],
         "read_path": ["mode"], "set": {"verb": "set_mode", "arg": "mode"}}]})
    r = _recipe(2)
    r.detectors = ["lockin_r", "state", "mode"]
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert list(ds["state"].values) == ["IDLE", "IDLE"]
    assert list(ds["mode"].values) == [0.0, 0.0]          # "fast" = option 0
    assert ds["mode"].attrs["flag_meanings"] == "fast slow"
    for det in ("state", "mode"):
        r = Recipe(name="t", axes=[{"type": "array", "param": det, "values": [0, 1]}],
                   detectors=["lockin_r"])
        assert any(det in e and "not settable" in e for e in r.validate(reg))


def test_an_untyped_text_detector_is_still_refused():
    from scan_core.registry import Gettable
    reg = build_sim_registry()
    reg.add(Gettable("legacy", "legacy", "", lambda: "x", dtype="text"))
    r = _recipe(2)
    r.detectors = ["lockin_r", "legacy"]
    assert any("legacy" in e and "text" in e for e in r.validate(reg))


# ---- 7. the suite must not swap the instruments under a running scan ---------

def test_switching_modules_is_refused_while_a_scan_runs(tmp_path, monkeypatch):
    """"Connect ticked", "Connect all running" and "Use simulator" closed the
    Lab -- the sockets the running scan was using, from the GUI thread while
    the scan thread sat in a request on them -- and dropped its axis stack.
    Following the launcher already waited for the scan; the buttons did not."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    import apps.control_panel as cp
    from apps.suite import Suite
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")

    class Busy:                               # a scan in progress
        _abort = False

        def isRunning(self):
            return True

    win = Suite()
    try:
        win.builder.add_axis("field")
        reg = win.registry
        win.builder.worker = Busy()
        win.use_simulator()
        win.connect_modules(["clMag"])
        assert win.registry is reg, "the registry was swapped under the scan"
        assert [r.param.id for r in win.builder.rows] == ["field"]
    finally:
        win.builder.worker = None
        win.close()

# ---- 8. a saved measurement says when it was taken ---------------------------

def test_a_saved_run_carries_its_real_start_time(tmp_path):
    """Every autosaved file said created = "live": the time lived only in the
    file NAME, and a copied or renamed file had none."""
    pytest.importorskip("PySide6")
    import os
    from datetime import datetime
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanWorker
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    path = tmp_path / "run.nc"
    ScanWorker(_recipe(2), build_sim_registry(), save_path=path).run()
    with xr.open_dataset(path) as ds:
        created = ds.attrs["created"]
    stamp = datetime.fromisoformat(created)        # raises on "live"
    assert abs((datetime.now() - stamp).total_seconds()) < 120

# ---- 9. `scale` applies to a value FETCHED with a command too ----------------

def test_a_descriptor_scale_applies_to_a_command_read():
    """wire = display x scale. The status reader divided by it, the `read`
    (command) reader did not -- the same descriptor then gave two numbers a
    factor `scale` apart depending on how the module chose to serve it."""
    inst = _StubInst({"p_W": 0.002}, replies={"get_power": {"p_W": 0.002},
                                              "get_trace": {"t": [0.001, 0.003]}})
    reg = Registry()
    register_manifest(reg, inst, {"module": "m", "parameters": [
        {"id": "p_status", "kind": "indicator", "type": "float", "unit": "mW",
         "scale": 1e-3, "read_path": ["p_W"]},
        {"id": "p_cmd", "kind": "indicator", "type": "float", "unit": "mW",
         "scale": 1e-3, "read": {"verb": "get_power", "key": "p_W"}},
        {"id": "trace", "kind": "indicator", "type": "array", "unit": "mW",
         "scale": 1e-3, "dims": [{"name": "i", "values": [0, 1]}],
         "read": {"verb": "get_trace", "key": "t"}}]})
    assert reg.get("p_status").get() == pytest.approx(2.0)
    assert reg.get("p_cmd").get() == pytest.approx(2.0)
    assert np.allclose(reg.get("trace").get(), [1.0, 3.0])


# ---- 10. a condition that is not a number is reported, not raised ------------

def test_a_non_numeric_condition_is_a_validation_error_not_a_crash():
    reg = build_sim_registry()
    r = _recipe(2)
    r.fixed = {"rf_power": "ten"}
    errs = r.validate(reg)                  # raised ValueError before
    assert any("rf_power" in e and "number" in e for e in errs), errs

# ---- 11. the live result pane shows the CURRENT coordinates ------------------

def test_the_live_pane_rows_show_the_new_scans_coordinates():
    """A new scan (or file) of the same SHAPE but other coordinate values kept
    the old rows -- the map was the 6 GHz slice while its row said 1 GHz.
    The operator's choice (slice #2) must survive, with the new label."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def cube(freqs):
        z = np.arange(3 * 2 * 4, dtype=float).reshape(3, 2, 4)
        return xr.Dataset({"sig": (("f", "y", "x"), z)},
                          coords={"f": ("f", freqs, {"units": "GHz"}),
                                  "y": [0.0, 1.0], "x": [0.0, 1.0, 2.0, 3.0]})

    v = DataView(); v.resize(700, 500); v.show()
    try:
        v.set_dataset(cube([1.0, 2.0, 3.0]))
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("y")
        app.processEvents()
        (row,) = v._rows
        assert row.dim == "f"
        row.slider.setValue(2)
        assert row.value.text() == "3 GHz"

        v.set_dataset(cube([4.0, 5.0, 6.0]))           # same shape, new values
        app.processEvents()
        (row,) = v._rows
        assert row.slider.value() == 2                 # the operator's choice kept
        assert row.value.text() == "6 GHz", row.value.text()
    finally:
        v.close(); v.deleteLater()


# ---- 12. closing the window during a scan ------------------------------------

def test_closing_the_suite_during_a_scan_aborts_it_first(tmp_path, monkeypatch):
    """Closing tore the connections down under the running scan and left its
    thread running (no after-scan routine, a QThread destroyed while running).
    Now the scan is aborted and waited for, and the after-scan routine runs."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    import apps.control_panel as cp
    from apps.suite import Suite
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")

    win = Suite()
    b = win.builder
    b.ask_before_unsaved = False
    b.autosave_dir = tmp_path
    b.add_axis("field")
    b.rows[0].num.setValue(400)
    b.add_routine_set("after_scan", "rf_power", -30.0)
    # 20 ms per point: the scan is certainly still running when we close
    win.registry.get("field")._set = (lambda v, s=win.registry.get("field")._set:
                                      (time.sleep(0.02), s(v)))
    b.run_scan()
    worker = b.worker
    t_end = time.monotonic() + 5
    while b.progress.value() < 3 and time.monotonic() < t_end:
        app.processEvents(); time.sleep(0.01)
    assert worker.isRunning()
    win.close()
    assert not worker.isRunning(), "the scan was left running after the window closed"
    assert worker.outcome == "aborted"
    assert win.registry._state.rf_power_dBm == -30.0, "the after-scan routine did not run"

# ---- 13. a NaN setpoint must never reach an instrument -----------------------

def test_a_nan_setpoint_is_refused_not_clamped_to_the_upper_limit():
    """max(lo, min(hi, nan)) is `hi`: a NaN setpoint drove the knob to its
    UPPER LIMIT. A NaN arrives from a position read that failed (a missing
    status key reads as NaN) -- e.g. the fly scan's "stop where you are" on
    Abort, which would then have sent the stage to the end of its travel."""
    sent = []
    p = Settable("x", "X", "um", (-100.0, 100.0),
                 set_fn=sent.append, get_fn=lambda: float("nan"))
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            p.set(bad)
    assert sent == []
    assert p.set(250.0) == 100.0 and sent == [100.0]     # clamping itself unchanged