"""Watching a scan server, phase 2: what the lab sees, at the office.

Lukas 2026-10-06: "with the gui of a service... i was really hoping to have a
1:1 copy of what i see on the lab pc". The server hands out the submitted
definitions and run info (get_scan) and the plot choice of the suite on its
own PC (set_view / get_view); a watching suite shows the queue with every
definition read-only, can copy one into its Scan tab, and follows the lab's
view while "show what the lab shows" is ticked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe                                     # noqa: E402

from test_scan_server import OTHER_PC, raw, recipe, wait_for      # noqa: E402
from test_scan_server import server, client                       # noqa: E402,F401  (fixtures)
from test_scan_server_suite import pump, qapp, rig                # noqa: E402,F401  (fixtures)


# ─────────────────────────────── the server ─────────────────────────────────

def test_get_scan_hands_out_the_definitions_and_run_info(server, client):
    srv = server()
    c = client(srv)
    assert c.get_scan()["entries"] == []
    rev0 = c.status()["scan_rev"]
    c.submit_queue([("first", recipe(name="a", num=4)), ("second", recipe(name="b", num=3))],
                   attrs={"sample": "S7", "operator": "LF"})
    wait_for(lambda: c.status()["scan_rev"] > rev0)
    r = c.get_scan()
    assert [e["name"] for e in r["entries"]] == ["first", "second"]
    assert r["entries"][0]["attrs"]["sample"] == "S7"
    assert Recipe.from_dict(r["entries"][1]["recipe"]).axes[0]["num"] == 3
    assert r["started_by"]


def test_the_view_is_set_from_this_pc_only(server, client):
    srv = server()
    c = client(srv)
    st0 = c.status()["view_rev"]
    assert c.set_view({"detector": "slow", "x": "field"})["ok"]
    wait_for(lambda: c.status()["view_rev"] == st0 + 1)
    assert c.get_view()["view"] == {"detector": "slow", "x": "field"}
    # the same view again: no new revision
    assert c.set_view({"detector": "slow", "x": "field"})["view_rev"] == st0 + 1
    r = raw(srv, {"cmd": "set_view", "view": {"detector": "x"}, "client": OTHER_PC})
    assert not r["ok"] and r["refused"] == "not_this_pc"
    assert c.get_view()["view"]["detector"] == "slow"          # unchanged
    assert raw(srv, {"cmd": "get_view", "client": OTHER_PC})["ok"]   # reading: anyone


def test_definition_lines_read_like_the_scan_tab():
    from apps.scan_server_view import definition_lines
    r = Recipe(name="m", fixed={"rf_power": -10},
               axes=[{"type": "linear", "param": "field", "start": 70, "stop": 0, "num": 36}],
               detectors=["u", "s21"],
               hooks=[{"when": "before_scan", "action": "call",
                       "args": {"set": {"field": 190}, "action": "vna_reference"}}])
    lines = definition_lines(r.to_dict() if hasattr(r, "to_dict") else
                             __import__("json").loads(r.to_json()), {"sample": "S2"})
    assert ("run info", "sample: S2") in lines
    assert ("axes", "1. field  70 -> 0, 36 pts") in lines
    assert ("conditions", "rf_power = -10") in lines
    assert ("routines", "before scan: field = 190; run vna_reference") in lines
    assert ("detectors", "u, s21") in lines


# ──────────────────────────── the plot as data ──────────────────────────────

def _cube():
    f = np.linspace(0, 10, 5); x = np.arange(4.0); y = np.arange(3.0)
    a = np.random.default_rng(1).random((5, 4, 3))
    return xr.Dataset({"r": (("freq", "x", "y"), a), "p": (("freq", "x", "y"), -a)},
                      coords={"freq": f, "x": x, "y": y})


def test_view_state_round_trips_between_two_views(qapp):
    from apps.data_view import DataView
    a, b = DataView(), DataView()
    a.set_dataset(_cube()); b.set_dataset(_cube())
    seen = []
    a.view_changed.connect(lambda: seen.append(1))
    a.det_combo.setCurrentText("p")
    a.x_combo.setCurrentText("x"); a.y_combo.setCurrentText("y")
    row = next(r for r in a._rows if r.dim == "freq")
    row.slider.setValue(3)
    a.set_z_range(-0.5, -0.1)
    assert seen                                   # the operator's changes are announced
    st = a.view_state()
    seen_b = []
    b.view_changed.connect(lambda: seen_b.append(1))
    b.apply_view_state(st)
    assert not seen_b                             # applying is not an operator change
    assert b.view_state() == st
    assert b.det_combo.currentText() == "p" and b._z_manual == (-0.5, -0.1)


def test_a_view_that_comes_before_the_data_waits_for_it(qapp):
    from apps.data_view import DataView
    v = DataView()
    v.apply_view_state({"detector": "p", "x": "x", "y": "freq"})
    v.set_dataset(_cube())
    assert v.det_combo.currentText() == "p"
    assert (v.x_combo.currentText(), v.y_combo.currentText()) == ("x", "freq")


# ─────────────────────────── the watching suite ─────────────────────────────

def test_the_watcher_lists_the_queue_with_definitions_and_copies_one(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    assert not b.server_submit and b.server_box.isVisibleTo(b.right_pane)
    c.submit_queue([("first", recipe(name="a", num=60, dets=("slow",))),
                    ("second", recipe(name="b", num=7))],
                   attrs={"sample": "S7", "operator": "LF"})

    def texts():
        out = []
        for i in range(b.server_tree.topLevelItemCount()):
            top = b.server_tree.topLevelItem(i)
            out.append(top.text(0))
            out += [top.child(k).text(0).strip() for k in range(top.childCount())]
        return out
    pump(qapp, lambda: any("running" in t for t in texts()))
    t = texts()
    assert t[0].startswith("1. first") and "running" in t[0]
    assert "sample: S7" in t and "operator: LF" in t
    assert any(x.startswith("2. second") and "waiting" in x for x in t)
    assert "1. field  0 -> 50, 7 pts" in t
    # copy the SECOND definition into this PC's Scan tab
    b.server_tree.setCurrentItem(b.server_tree.topLevelItem(1))
    assert b.copy_def_btn.isEnabled()
    missing = b._copy_server_definition()
    assert missing == ["slow"]          # the server's own detector: not on this PC
    assert "not available on this PC: slow" in b.detail.text()
    assert b.name_edit.text() == "second"
    assert b.build_recipe().axes[0]["num"] == 7
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running)


def test_the_watcher_follows_the_labs_view_while_ticked(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    c.submit(recipe(num=60, dets=("lockin_r", "slow")))
    pump(qapp, lambda: b.view.ds is not None and b.det_combo.count() >= 2)
    first = b.det_combo.currentText()
    other = "slow" if first != "slow" else "lockin_r"
    c.set_view({"detector": other})                    # what the lab's suite would send
    pump(qapp, lambda: b.det_combo.currentText() == other)
    b.follow_view_box.setChecked(False)                # my own view from now on
    b.det_combo.setCurrentText(first)
    c.set_view({"detector": other, "x": "field"})
    pump(qapp, lambda: b.server.last_view.get("view", {}).get("x") == "field")
    qapp.processEvents()
    assert b.det_combo.currentText() == first          # not followed
    b.follow_view_box.setChecked(True)                 # ...and back: jumps to the lab's
    assert b.det_combo.currentText() == other
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running)


def test_the_suite_on_the_servers_pc_publishes_its_view(qapp, rig):
    srv, win, c = rig(settings={"run_on_scan_server": True})
    b = win.builder
    pump(qapp, lambda: b.server_submit)
    assert not b.server_box.isVisibleTo(b.right_pane)     # it IS the lab: nothing to mirror
    c.submit(recipe(num=60, dets=("lockin_r", "slow")))
    pump(qapp, lambda: b.view.ds is not None and b.det_combo.count() >= 2)
    want = "slow" if b.det_combo.currentText() != "slow" else "lockin_r"
    b.det_combo.setCurrentText(want)                     # the lab operator's choice
    pump(qapp, lambda: srv._view.get("detector") == want)
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running)
