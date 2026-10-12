"""Will the streams fill the pixels? (flyscan.sampling_estimate / fly_warnings,
Lukas 2026-10-11: "before starting a scan we calculate the approx time for a
sweep and if an empty pixel happens we inform the user ... we just warn").

  * BEFORE: from each module's declared stream rate (describe `rate_hz`) the
    samples per pixel are estimated; below one -> a WARNING that names the
    detector, the estimate and the speed that would do -- validate() still
    passes and the run goes ahead;
  * DURING: a row with EMPTY pixels is said once, and the scan ends with a
    count ("37 empty pixels in 12 of 40 rows").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.flyscan import fly_warnings, sampling_estimate, sampling_summary
from scan_core.registry import build_sim_registry
from scan_core.sim_stream import SimStreamer


def _fly(**kw):
    ax = {"type": "fly", "param": "pos_x", "start": -10, "stop": 10, "num": 41,
          "speed": 20, "speed_param": "stage_speed"}
    ax.update(kw)
    return ax


def _recipe(axes=None, detectors=("lockin_r",)):
    return Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
                  axes=list(axes if axes is not None else [_fly()]),
                  detectors=list(detectors))


@pytest.fixture
def reg():
    r = build_sim_registry()
    r._state.lockin_tc_s = 0.004
    return r


def test_the_estimate_uses_the_declared_rate(reg):
    # 20 um over 41 pixels at 20 um/s: 0.5 um = 25 ms a pixel; lock-in 200 / s
    est = sampling_estimate(_recipe(), reg)
    assert est["row_s"] == pytest.approx(41 * 0.025)
    assert est["pixel_s"] == pytest.approx(0.025)
    assert est["unit"] == "um/s"
    d = est["detectors"]["lockin_r"]
    assert d["rate_hz"] == 200.0 and d["per_pixel"] == pytest.approx(5.0)
    assert d["speed_for_1"] == pytest.approx(100.0)
    assert d["speed_for_3"] == pytest.approx(100.0 / 3)
    assert fly_warnings(_recipe(), reg) == []
    assert "lockin_r ~5" in sampling_summary(est)
    assert sampling_estimate(Recipe(name="x", axes=[{"type": "linear", "param": "pos_x",
                                                     "start": 0, "stop": 1, "num": 2}],
                                    detectors=["lockin_r"]), reg) is None


def test_too_fast_warns_with_the_speed_that_would_do_but_validates(reg):
    r = _recipe(axes=[_fly(speed=400)])              # 1.25 ms a pixel: 0.25 samples
    assert r.validate(reg) == []                     # never blocks
    w = fly_warnings(r, reg)
    assert len(w) == 1
    assert "lockin_r" in w[0] and "0.25 samples per bin" in w[0] and "EMPTY" in w[0]
    assert "<= 100 um/s for >= 1" in w[0] and "<= 33.3 um/s for >= 3" in w[0]


def test_a_stream_without_a_declared_rate_is_not_guessed(reg):
    reg.get("lockin_r").stream_rate_hz = None
    est = sampling_estimate(_recipe(axes=[_fly(speed=400)]), reg)
    assert est["detectors"]["lockin_r"]["per_pixel"] is None
    assert fly_warnings(_recipe(axes=[_fly(speed=400)]), reg) == []
    assert "rate not declared" in sampling_summary(est)


def test_a_stream_that_delivers_nothing_says_so(reg):
    reg.get("lockin_r").stream_rate_hz = 0.0
    w = fly_warnings(_recipe(), reg)
    assert len(w) == 1 and "NOTHING" in w[0]


def _slow_stream(reg, rate=3.0):
    """lockin_r recorded by a deliberately SLOW stream (an oscilloscope's
    records at a slow time base): far fewer samples than pixels."""
    s = reg._state
    slow = SimStreamer("sim.slow", lambda: {"R": float(np.hypot(*[s.lockin()[k]
                                                                  for k in ("x", "y")]))},
                       rate_hz=rate)
    p = reg.get("lockin_r")
    p.stream, p.stream_channel, p.stream_rate_hz = slow.spec(), "R", slow.rate_hz
    return slow


def test_empty_pixels_are_warned_and_summed_up_but_the_scan_runs(reg):
    _slow_stream(reg, rate=3.0)
    # 10 um at 40 um/s: ~0.26 s a row, 21 pixels; at 3 / s about one sample a row
    r = _recipe(axes=[{"type": "linear", "param": "pos_y", "start": -2, "stop": 2,
                       "num": 3},
                      _fly(start=-5, stop=5, num=21, speed=40)])
    assert r.validate(reg) == []
    log = []
    ds = run(r, reg, on_log=log.append)
    n = ds["lockin_r_n"].values
    assert n.shape == (3, 21) and np.sum(n == 0) > 20
    text = "\n".join(log)
    # before: the estimate and the warning
    assert "expected samples per bin: lockin_r ~0.0" in text
    assert "fly: WARNING: lockin_r: ~0.0" in text and "bins will be EMPTY" in text
    # during: the first row with empty pixels, ONCE
    first = [m for m in log if "bins EMPTY on row" in m]
    assert len(first) == 1 and "lockin_r" in first[0]
    # after: the count over the whole scan
    end = [m for m in log if "empty bin(s) in" in m]
    assert len(end) == 1
    assert f"{int(np.sum(n == 0))} empty bin(s) in 3 of 3 rows" in end[0]
    assert "slow down the fly speed" in end[0]


def test_no_empty_pixel_no_warning(reg):
    log = []
    run(_recipe(axes=[_fly(start=-5, stop=5, num=11, speed=20)]), reg, on_log=log.append)
    assert not any("WARNING" in m for m in log), log
    assert any("expected samples per bin: lockin_r ~" in m for m in log)


def test_a_module_saying_missed_samples_is_warned(reg):
    """An oscilloscope's stream says how many records went by unread
    (`missed`): the scan says it once, and in the end summary."""
    slow = _slow_stream(reg, rate=50.0)
    inner = slow.read

    def read_with_missed():
        c = inner()
        c["missed"] = 2
        return c
    from scan_core.registry import StreamSpec
    p = reg.get("lockin_r")
    p.stream = StreamSpec("sim.slow", slow.start, read_with_missed, slow.stop)
    log = []
    run(_recipe(axes=[_fly(start=-5, stop=5, num=11, speed=20)]), reg, on_log=log.append)
    assert sum("went by unread on row" in m for m in log) == 1
    assert any("went by unread during the scan" in m for m in log)


# ---- the wire: describe -> registry, and the scan server's manifest -----------------

def test_rate_hz_is_read_from_describe():
    from scan_core.manifest import _attach_stream
    from scan_core.registry import Gettable

    p = Gettable("scope.ch1_pk2pk", "CH1 pk-pk", "V", lambda: 0.0)
    streams = {}
    _attach_stream(p, {"id": "ch1_pk2pk", "stream": {"group": "records",
                                                     "channel": "ch1_pk2pk",
                                                     "rate_hz": 2.5}},
                   None, "scope", streams, None)
    assert p.stream_rate_hz == 2.5 and p.stream_channel == "ch1_pk2pk"
    q = Gettable("scope.ch1_rms", "CH1 rms", "V", lambda: 0.0)
    _attach_stream(q, {"id": "ch1_rms", "stream": {"group": "records",
                                                   "channel": "ch1_rms"}},
                   None, "scope", streams, None)
    assert q.stream_rate_hz is None and q.stream is p.stream


def test_rate_hz_travels_through_the_scan_server_manifest(reg):
    from scan_core.registry_wire import registry_from_wire, registry_to_wire
    wire = registry_to_wire(reg)
    back = registry_from_wire(wire)
    assert back.get("lockin_r").stream_rate_hz == 200.0
    assert back.get("pos_x").stream_rate_hz == 500.0


# ---- the Scan Builder ---------------------------------------------------------------

def test_the_builder_warns_but_keeps_run_enabled():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    win = ScanBuilder(reg)
    try:
        for it in win._det_items():
            it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                             == "lockin_r" else QtCore.Qt.Unchecked)
        win.add_axis("pos_x")
        row = win.rows[-1]
        row.start.setValue(-10.0)
        row.stop.setValue(10.0)
        row.num.setValue(41)
        win.open_advanced(row)
        row.fly.setChecked(True)
        row.speed.setValue(20.0)
        win._rebuild_summary()
        text = win.detail.text()
        assert "samples/bin (lockin_r)" in text and "WARNING" not in text
        row.speed.setValue(400.0)
        win._rebuild_summary()
        text = win.detail.text()
        assert "WARNING: lockin_r" in text and "bins will be EMPTY" in text
        assert win.summary.text() != "invalid"
        assert win.run_btn.isEnabled()
    finally:
        win.close()
