"""DIAGONAL row changes (2026-10-08, from the first rig test of the XY mask).

At the start of a new row two axes change. Set one after the other, the camera
first settles at (last column, next row) -- a point that is never recorded,
one wasted stabiliser settle (~3 s) per row, and the camera GUI marks it as
visited. With `diagonal: true` every new setpoint is SENT first and only then
are they all waited for, so the move goes straight to (first column, next row).

  * the split set (Settable.send / can_send) in the simulator and over the
    manifest (a stub instrument: command and wait recorded in order);
  * the engine: send-send-wait-wait at a row change, plain sets elsewhere,
    fallback where a knob cannot split, axis hooks unchanged, the data
    identical; the mask's pass 1 does the same;
  * the simulator's stage at a finite speed: the row change costs the LONGER
    of the two moves, not their sum.
No ports are used.
"""

from __future__ import annotations

import time

import numpy as np

from scan_core import Recipe, run
from scan_core.manifest import register_manifest
from scan_core.registry import Registry, Settable, Gettable, build_sim_registry


def _logged_registry(can_send=True):
    """Two knobs x / y that log every send, wait and set, and a detector."""
    log = []
    reg = Registry()
    for pid in ("x", "y"):
        def set_fn(v, _p=pid):
            log.append(("set", _p, float(v)))

        def send_fn(v, _p=pid):
            log.append(("send", _p, float(v)))
            return lambda: log.append(("wait", _p, float(v)))
        reg.add(Settable(pid, pid, "um", (-100, 100), set_fn=set_fn,
                         get_fn=lambda: 0.0, send_fn=send_fn if can_send else None))
    reg.add(Gettable("d", "d", "", lambda: 1.0))
    return reg, log


def _grid(**kw):
    return Recipe(axes=[{"type": "raster",
                         "x": {"param": "x", "start": 0, "stop": 2, "num": 3},
                         "y": {"param": "y", "start": 0, "stop": 1, "num": 2}}],
                  detectors=["d"], **kw)


def test_settable_send_clamps_and_returns_a_wait():
    reg, log = _logged_registry()
    value, wait = reg.get("x").send(500.0)
    assert value == 100.0 and log == [("send", "x", 100.0)]
    wait()
    assert log[-1] == ("wait", "x", 100.0)
    assert reg.get("x").can_send
    assert not build_sim_registry().get("field").can_send


def test_row_change_sends_both_then_waits():
    reg, log = _logged_registry()
    run(_grid(diagonal=True), reg)
    # the first point and the row change: both sent, then both waited for
    assert log[:4] == [("send", "y", 0.0), ("send", "x", 0.0),
                       ("wait", "y", 0.0), ("wait", "x", 0.0)]
    row2 = log.index(("send", "y", 1.0))
    assert log[row2:row2 + 4] == [("send", "y", 1.0), ("send", "x", 0.0),
                                  ("wait", "y", 1.0), ("wait", "x", 0.0)]
    # inside a row only x moves: an ordinary blocking set
    assert ("set", "x", 1.0) in log and ("set", "x", 2.0) in log


def test_without_the_flag_nothing_changes():
    reg, log = _logged_registry()
    run(_grid(), reg)
    assert all(e[0] == "set" for e in log)
    i = log.index(("set", "y", 1.0))
    assert log[i:i + 2] == [("set", "y", 1.0), ("set", "x", 0.0)]   # outer first


def test_a_knob_that_cannot_split_falls_back():
    reg, log = _logged_registry(can_send=False)
    ds = run(_grid(diagonal=True), reg)
    assert all(e[0] == "set" for e in log)
    assert np.isfinite(ds["d"].values).all()


def test_axis_hooks_fire_the_same_number_of_times():
    def count(diagonal):
        reg, _ = _logged_registry()
        r = _grid(diagonal=diagonal)
        r.hooks = [{"when": w, "axis": a, "action": "call",
                    "args": {"steps": [{"comment": {"text": f"{w} {a}"}}]}}
                   for w in ("before_axis", "after_axis") for a in ("x", "y")]
        import json
        ds = run(r, reg)
        return sorted(c["text"] for c in json.loads(ds.attrs["comments"]))
    assert count(True) == count(False)


def test_the_data_is_identical():
    a = run(_grid(), _logged_registry()[0])
    b = run(_grid(diagonal=True), _logged_registry()[0])
    assert (a["d"].values == b["d"].values).all()
    assert "diagonal" not in _grid().to_dict() and _grid(diagonal=True).to_dict()["diagonal"]


def test_the_mask_pass_moves_diagonally_too():
    reg = build_sim_registry()
    moves = []
    for pid in ("pos_x", "pos_y"):
        p = reg.get(pid)
        orig = p._send

        def send(v, _o=orig, _p=pid):
            moves.append(("send", _p, v))
            return _o(v)
        p._send = send
    r = Recipe(axes=[{"type": "raster",
                      "x": {"param": "pos_x", "start": -45, "stop": 45, "num": 31},
                      "y": {"param": "pos_y", "start": -45, "stop": 45, "num": 31}}],
               detectors=["lockin_r"], diagonal=True,
               mask={"detector": "reflectivity", "step": 3})
    run(r, reg)
    # pass 1 has 11 rows: at least 10 row changes with both axes sent
    ys = [m for m in moves if m[1] == "pos_y"]
    assert len(ys) >= 10


def test_a_finite_speed_stage_pays_the_longer_move_not_the_sum():
    def seconds(diagonal):
        reg = build_sim_registry()
        r = Recipe(fixed={"stage_speed": 400.0},
                   axes=[{"type": "raster",
                          "x": {"param": "pos_x", "start": 0, "stop": 40, "num": 2},
                          "y": {"param": "pos_y", "start": 0, "stop": 80, "num": 3}}],
                   detectors=["lockin_r"], diagonal=diagonal)
        t = time.monotonic()
        run(r, reg)
        return time.monotonic() - t
    # each of 2 row changes: y 40 um + x 40 um back = 0.2 s one after the other,
    # 0.1 s together -> about 0.2 s saved in all
    plain, diag = seconds(False), seconds(True)
    assert plain - diag > 0.1, (plain, diag)


class _WaitStub:
    """Enough of an Instrument for register_manifest: logs commands and waits."""
    name = "cam"

    def __init__(self):
        self.log, self.st = [], {"ix": 0, "iy": 0}

    def status(self):
        return dict(self.st)

    def command(self, verb, **kw):
        self.log.append(("cmd", verb, kw))
        self.st.update(kw)
        return {"ok": True}

    def wait_until(self, predicate, timeout_s=None, what="", cancel=None):
        self.log.append(("wait", what))
        assert predicate(self.status())


def test_the_lab_setter_splits_into_send_and_wait():
    inst = _WaitStub()
    reg = Registry()
    register_manifest(reg, inst, {"module": "cam", "parameters": [
        {"id": p, "kind": "control", "type": "int", "min": 0, "max": 29,
         "read_path": [p], "set": {"verb": f"set_{p}", "arg": p},
         "settle": {"policy": "echoes", "key": p}} for p in ("ix", "iy")]})
    x = reg.get("ix")
    assert x.can_send
    _, wait = x.send(3)
    assert inst.log == [("cmd", "set_ix", {"ix": 3})]     # sent, not yet waited
    wait()
    assert inst.log[-1][0] == "wait"
    inst.log.clear()
    x.set(5)                                                # set = send + wait
    assert [e[0] for e in inst.log] == ["cmd", "wait"]
