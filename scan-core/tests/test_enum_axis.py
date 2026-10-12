"""Scanning a NON-numeric knob (2026-10-12): an enum (a filter wheel, a
grating, a lock-in's input mode) or a string control is a settable; an
`array` axis carries its options as they are, the engine never float()s
them, the settle compares exactly, validate checks them against the
options, and the data file stores a TEXT coordinate that reads back.
"""

from __future__ import annotations

import copy
import threading
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.registry import Gettable, Registry, Settable

from conftest import DEMO_MANIFEST, FakeService

INPUTS = ["A", "B", "A-B"]


# ───────────────────────────── in memory ──────────────────────────────────

def _registry():
    """A sim-like registry with an enum knob, a string knob and a detector
    that depends on the enum."""
    state = {"input": "A", "label": "", "sent": []}
    reg = Registry()

    def set_input(v):
        assert isinstance(v, str), f"the engine float()ed an enum: {v!r}"
        state["sent"].append(v)
        state["input"] = v

    reg.add(Settable("input", "Input", "", (float("-inf"), float("inf")),
                     set_input, lambda: state["input"], dtype="enum", options=INPUTS))
    reg.add(Settable("label", "Label", "", (float("-inf"), float("inf")),
                     lambda v: state.__setitem__("label", v), lambda: state["label"],
                     dtype="string"))
    reg.add(Settable("x", "X", "um", (-10, 10), lambda v: None, lambda: 0.0))
    reg.add(Gettable("sig", "Signal", "V",
                     lambda: float(INPUTS.index(state["input"]) + 1)))
    return reg, state


def test_an_array_axis_over_enum_options_runs_and_keeps_the_text():
    reg, state = _registry()
    r = Recipe(axes=[{"type": "array", "param": "input", "values": INPUTS},
                     {"type": "linear", "param": "x", "start": 0, "stop": 1, "num": 2}],
               detectors=["sig"], fixed={"label": "sample 7"})
    assert r.validate(reg) == []
    ds = run(r, reg, created_iso="t")
    assert list(ds["input"].values) == INPUTS
    assert ds["sig"].values[:, 0].tolist() == [1.0, 2.0, 3.0]
    assert state["sent"] == INPUTS                   # sent as the options themselves
    assert ds.coords["label"].item() == "sample 7"   # a text condition


def test_validate_checks_the_options():
    reg, _ = _registry()
    bad = Recipe(axes=[{"type": "array", "param": "input", "values": ["A", "C"]}],
                 detectors=["sig"])
    assert any("'C' is not one of its options" in e for e in bad.validate(reg))
    # numbers are refused for a numeric knob given text, and the other way round
    wrong = Recipe(axes=[{"type": "array", "param": "x", "values": [0, "far"]}],
                   detectors=["sig"])
    assert any("takes numbers" in e for e in wrong.validate(reg))
    lin = Recipe(axes=[{"type": "linear", "param": "input", "start": 0, "stop": 2, "num": 3}],
                 detectors=["sig"])
    assert lin.validate(reg)                          # 0.0, 1.0, 2.0 are no options
    cond = Recipe(axes=[{"type": "linear", "param": "x", "start": 0, "stop": 1, "num": 2}],
                  detectors=["sig"], fixed={"input": "Z"})
    assert any("'Z' is not one of its options" in e for e in cond.validate(reg))
    routine = Recipe(axes=[{"type": "linear", "param": "x", "start": 0, "stop": 1, "num": 2}],
                     detectors=["sig"],
                     hooks=[{"when": "before_scan", "action": "call",
                             "args": {"set": {"input": "B"}}}])
    assert routine.validate(reg) == []


def test_a_routine_sets_and_restores_an_enum():
    reg, state = _registry()
    r = Recipe(axes=[{"type": "array", "param": "input", "values": ["A"]},
                     {"type": "linear", "param": "x", "start": 0, "stop": 1, "num": 2}],
               detectors=["sig"],
               hooks=[{"when": "each_sweep", "axis": "x", "edge": "start",
                       "action": "call", "args": {"set": {"input": "B"}}}])
    run(r, reg, created_iso="t")
    # set to B by the routine, put back to A (the axis value) after it
    assert state["sent"] == ["A", "B", "A"]


def test_the_text_coordinate_survives_the_file(tmp_path):
    reg, _ = _registry()
    r = Recipe(axes=[{"type": "array", "param": "input", "values": INPUTS}],
               detectors=["sig"])
    ds = run(r, reg, created_iso="t")
    path = tmp_path / "enum.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    from scan_core.data import load, summarize
    back = load(path)
    try:
        assert [str(v) for v in back["input"].values] == INPUTS
        assert back["sig"].values.tolist() == [1.0, 2.0, 3.0]
        summarize(path)                               # AaltoView's file summary copes
    finally:
        back.close()


def test_a_file_axis_may_hold_text(tmp_path):
    reg, _ = _registry()
    f = tmp_path / "inputs.txt"
    f.write_text("A\nA-B\n", encoding="utf-8")
    r = Recipe(axes=[{"type": "file", "param": "input", "path": str(f)}], detectors=["sig"])
    assert r.validate(reg) == []
    assert list(run(r, reg, created_iso="t")["input"].values) == ["A", "A-B"]


def test_an_old_raster_axis_is_read_as_a_grid(tmp_path):
    """`raster` (until 2026-10-12, "spatial XY") is the 2-D `grid` of any
    two knobs; an old definition is translated when it is read."""
    from scan_core import build_sim_registry
    old = {"type": "raster", "x": {"param": "field", "start": 0, "stop": 10, "num": 3},
           "y": {"param": "rf_freq", "start": 1000, "stop": 2000, "num": 2}, "fast": "x"}
    for r in (Recipe(axes=[dict(old)], detectors=["lockin_r"]),
              Recipe.from_dict({"axes": [dict(old)], "detectors": ["lockin_r"]})):
        assert r.axes[0]["type"] == "grid"
        assert r.validate(build_sim_registry()) == []
        assert [d.name for d in r.compile().dims] == ["rf_freq", "field"]
    r.save(tmp_path / "g.yaml")
    assert "raster" not in (tmp_path / "g.yaml").read_text(encoding="utf-8")


def test_the_yaml_round_trip_keeps_text_values(tmp_path):
    r = Recipe(axes=[{"type": "array", "param": "input", "values": INPUTS}], detectors=["sig"])
    r.save(tmp_path / "e.yaml")
    assert Recipe.load(tmp_path / "e.yaml").axes == r.axes


# ───────────────────────────── over the wire ──────────────────────────────

class _EnumService(FakeService):
    """The fake clMag plus a lock-in-like INPUT selector: an enum control
    whose setting is echoed (late, fire-and-forget) in status."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._input = "A"
        self.inputs_sent: list = []

    def _status(self):
        st = super()._status()
        with self._lock:
            st["input"] = self._input
            st["signal"] = float(INPUTS.index(self._input) + 1)
        return st

    def _handle(self, msg):
        if msg.get("cmd") == "set_input":
            v = msg["input"]
            self.inputs_sent.append(v)
            if v not in INPUTS:
                return {"ok": False, "error": f"no input {v!r}"}

            def adopt():
                time.sleep(0.1)
                with self._lock:
                    self._input = v
            threading.Thread(target=adopt, daemon=True).start()
            return {"ok": True}
        return super()._handle(msg)


def _manifest():
    m = copy.deepcopy(DEMO_MANIFEST)
    m["parameters"] += [
        {"id": "input", "label": "Input", "kind": "control", "type": "enum",
         "options": INPUTS, "read_path": ["input"],
         "set": {"verb": "set_input", "arg": "input"},
         "settle": {"policy": "echoes", "key": "input"}, "timeout_s": 5},
        {"id": "signal", "label": "Signal", "kind": "indicator", "type": "float",
         "unit": "V", "read_path": ["signal"]}]
    return m


def test_an_enum_control_of_a_service_is_scanned_over_the_wire(tmp_path):
    from scan_core.lab import build_lab_registry
    svc = _EnumService(15924, manifest=_manifest()).start()
    try:
        reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                      ports={"clMag": svc.cmd_port})
        try:
            p = reg.get("input")
            assert isinstance(p, Settable) and p.options == INPUTS and not p.numeric
            mode = reg.get("mode")                      # DEMO's enum: settable now too
            assert isinstance(mode, Settable) and mode.options == ["hardware", "software", "off"]
            r = Recipe(axes=[{"type": "array", "param": "input", "values": ["B", "A-B", "A"]}],
                       detectors=["signal"])
            assert r.validate(reg) == []
            ds = run(r, reg, created_iso="t")
            # each point waited for the ECHO of its option (exact compare),
            # so the signal belongs to the option it is filed under
            assert svc.inputs_sent == ["B", "A-B", "A"]
            assert ds["signal"].values.tolist() == [2.0, 3.0, 1.0]
            ds.to_netcdf(tmp_path / "wire.nc", engine="h5netcdf")
            from scan_core.data import load
            back = load(tmp_path / "wire.nc")
            try:
                assert [str(v) for v in back["input"].values] == ["B", "A-B", "A"]
            finally:
                back.close()
        finally:
            lab.close()
    finally:
        svc.stop()


def test_the_scan_server_proxy_carries_options():
    from scan_core.registry_wire import registry_from_wire, registry_to_wire
    reg, _ = _registry()
    proxy = registry_from_wire(registry_to_wire(reg))
    p = proxy.get("input")
    assert p.dtype == "enum" and p.options == INPUTS and not p.numeric
    assert proxy.get("label").dtype == "string"
    assert np.isinf(proxy.get("input").limits[1])
