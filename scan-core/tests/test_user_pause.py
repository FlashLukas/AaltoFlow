"""The OPERATOR's Pause / Resume (Lukas, 2026-10-07: a "pause button" next to
Run and Abort).

engine.run(..., should_pause=) holds the scan BETWEEN points (a fly scan:
between rows) -- the places Abort is checked -- so the point in progress is
always finished first. Abort while held is an ordinary Abort; should_pause=None
is the old behaviour exactly. Offline; no sockets (the scan server's verbs are
in test_scan_server.py, its watching GUI in test_scan_server_suite.py).
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.registry import Action, Gettable, Registry, Settable, build_sim_registry


def _registry():
    """A knob `b`, a knob `c` (for the after-scan routine) and a detector
    that writes down WHEN it was read."""
    state = {"b": 0.0, "c": 99.0}
    reads: list = []
    reg = Registry()
    for pid in ("b", "c"):
        reg.add(Settable(pid, pid, "mT", (-500, 500),
                         lambda v, _p=pid: state.__setitem__(_p, v),
                         lambda _p=pid: state[_p]))

    def read():
        reads.append(time.monotonic())
        return state["b"]
    reg.add(Gettable("b_now", "b as measured", "mT", read))
    reg.add_action(Action("noop", "noop", lambda: None))
    return reg, state, reads


def _recipe(n=5, hooks=()):
    return Recipe(name="p", axes=[{"type": "array", "param": "b",
                                   "values": list(range(1, n + 1))}],
                  detectors=["b_now"], hooks=list(hooks))


def test_a_paused_scan_measures_nothing_until_resumed_and_then_finishes():
    reg, _, reads = _registry()
    held = {}

    def should_pause():
        # pressed after the 2nd point; released 0.6 s later
        if len(reads) >= 2:
            held.setdefault("t", time.monotonic())
            return time.monotonic() - held["t"] < 0.6
        return False

    logs, etas = [], {}
    ds = run(_recipe(), reg, should_pause=should_pause, on_log=logs.append,
             on_progress=lambda d, n, eta: etas.__setitem__(d, eta))
    assert np.all(np.isfinite(ds["b_now"].values)) and ds["b_now"].size == 5
    # nothing was read while held: the gap sits between point 2 and point 3
    assert reads[2] - reads[1] >= 0.55
    assert all(reads[i + 1] - reads[i] < 0.3 for i in (0, 2, 3))
    assert "PAUSED by the operator before point 3" in logs
    assert "resumed" in logs
    # the pause is not counted as measuring time: with 0.6 s in the elapsed
    # time the ETA at point 3 of 5 would be >= 0.4 s
    assert etas[3] < 0.2


def test_abort_while_paused_is_an_ordinary_abort():
    """The after-scan routine runs and the points measured so far are kept."""
    reg, state, reads = _registry()
    t = {}

    def should_pause():
        return len(reads) >= 2                     # held for good after point 2

    def should_abort():
        if len(reads) >= 2:
            t.setdefault("t", time.monotonic())
            return time.monotonic() - t["t"] > 0.3
        return False

    logs = []
    ds = run(_recipe(hooks=[{"when": "after_scan", "action": "call",
                             "args": {"set": {"c": 0.0}}}]),
             reg, should_pause=should_pause, should_abort=should_abort,
             on_log=logs.append)
    vals = ds["b_now"].values
    assert np.isfinite(vals[:2]).all() and np.isnan(vals[2:]).all()
    assert state["c"] == 0.0                       # after_scan ran
    assert "aborted while paused" in logs
    assert "resumed" not in logs


def test_without_should_pause_nothing_changes():
    reg, _, _ = _registry()
    logs = []
    a = run(_recipe(), reg, on_log=logs.append)
    b = run(_recipe(), reg, should_pause=None, on_log=logs.append)
    c = run(_recipe(), reg, should_pause=lambda: False, on_log=logs.append)
    for ds in (b, c):
        np.testing.assert_array_equal(a["b_now"].values, ds["b_now"].values)
    assert not any("PAUSED" in m for m in logs)


def test_a_fly_scan_pauses_between_rows_and_the_stage_stays_put():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
               axes=[{"type": "linear", "param": "pos_y", "start": -1, "stop": 1, "num": 3},
                     {"type": "fly", "param": "pos_x", "start": -10, "stop": 10,
                      "num": 21, "speed": 40, "speed_param": "stage_speed"}],
               detectors=["lockin_r"])
    rows = {"done": 0}
    held = {"x": []}

    def on_progress(done, total, eta):
        rows["done"] = done // 21

    def should_pause():
        if rows["done"] == 1:                       # after the first row
            held.setdefault("t", time.monotonic())
            if time.monotonic() - held["t"] < 0.5:
                held["x"].append(reg._state.x_um)
                return True
        return False

    logs = []
    ds = run(r, reg, should_pause=should_pause, on_log=logs.append,
             on_progress=on_progress)
    assert "PAUSED by the operator before row 2 of 3" in logs and "resumed" in logs
    assert np.all(np.isfinite(ds["lockin_r"].values))
    # held at the end of row 1: the stage did not wander off while paused
    assert len(held["x"]) >= 3 and max(held["x"]) - min(held["x"]) < 1e-6


# ─────────────────────────── the GUI: the Pause button ────────────────────────

def _builder():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    # 30 ms per point, so the scan is still running when the button is pressed
    field = reg.get("field")
    fast_set = field._set
    field._set = lambda v: (time.sleep(0.03), fast_set(v))[1]
    win = ScanBuilder(reg)
    win.add_axis("field")
    win.rows[0].num.setValue(40)
    return win


def _pump_until(cond, timeout=10.0):
    from PySide6 import QtWidgets
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_pause_button_toggles_and_drives_the_worker():
    win = _builder()
    calls = []
    try:
        assert not win.pause_btn.isEnabled()          # nothing runs yet
        assert win.pause_btn.objectName() == ""       # the plain button of the theme
        assert "Abort still works while paused" in win.pause_btn.toolTip()
        w = win._start_worker(win.build_recipe())
        orig_pause, orig_resume = w.pause, w.resume
        w.pause = lambda: (calls.append("pause"), orig_pause())
        w.resume = lambda: (calls.append("resume"), orig_resume())
        assert win.pause_btn.isEnabled() and "Pause" in win.pause_btn.text()
        win.pause_btn.click()
        assert calls == ["pause"] and w._pause
        assert "Resume" in win.pause_btn.text() and win.pause_btn.isEnabled()
        assert win.run_status_text().startswith("PAUSED")
        assert "PAUSED" in win.progress.format()
        # the engine really holds: the count stops moving
        assert _pump_until(lambda: any("PAUSED by the operator" in m for m in win.run_log))
        n = win.progress.value()
        _pump_until(lambda: False, timeout=0.4)
        assert win.progress.value() == n and win.worker is not None
        assert win.abort_btn.isEnabled()              # Abort works while paused
        win.pause_btn.click()
        assert calls == ["pause", "resume"] and not w._pause
        assert "Pause" in win.pause_btn.text() and "Resume" not in win.pause_btn.text()
        assert _pump_until(lambda: win.worker is None, timeout=30.0)
        assert any(m == "resumed" for m in win.run_log)
        # the run is over: back to Pause, and off
        assert not win.pause_btn.isEnabled() and "Pause" in win.pause_btn.text()
        assert np.all(np.isfinite(win.dataset["lockin_r"].values))
    finally:
        win.close()


def test_abort_while_paused_from_the_gui_resets_the_button():
    win = _builder()
    try:
        win._start_worker(win.build_recipe())
        assert _pump_until(lambda: win.progress.value() >= 2)
        win.pause_btn.click()
        assert _pump_until(lambda: any("PAUSED by the operator" in m for m in win.run_log))
        win.abort_btn.click()
        assert _pump_until(lambda: win.worker is None, timeout=15.0)
        assert not win.pause_btn.isEnabled() and "Pause" in win.pause_btn.text()
        assert not win.is_user_paused()
        vals = win.dataset["lockin_r"].values
        assert np.isfinite(vals).any() and np.isnan(vals).any()
    finally:
        win.close()
