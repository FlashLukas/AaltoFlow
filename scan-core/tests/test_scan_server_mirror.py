"""Watching a scan server, phase 2: what the lab sees, at the office.

Lukas 2026-10-06: "with the gui of a service... i was really hoping to have a
1:1 copy of what i see on the lab pc". The server hands out the submitted
definitions and run info (get_scan) and the plot choice of the suite on its
own PC (set_view / get_view); a watching suite shows the queue with every
definition read-only, can copy one into its Scan tab, and follows the lab's
view while "show what the lab shows" is ticked.
"""

from __future__ import annotations

import json
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
    pump(qapp, lambda: (srv._view.get("plot") or {}).get("detector") == want)
    assert "pids" in srv._view["panel"]              # the Control tab's panel goes too
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running)


# ─────────── a server on ANOTHER PC: its instruments, layouts, title ────────

def test_watching_another_pc_shows_its_instruments_layouts_and_panel(
        qapp, rig, fake_service, monkeypatch):
    """Lukas 2026-10-06, office and lab suites side by side: "they are very
    different". Watching a server on another PC, the suite takes THAT
    server's instruments under the same names, the lab's layouts (read-only)
    and title, follows the lab's Control-tab panel, and gives it all back on
    Stop watching."""
    from conftest import DEMO_MANIFEST
    import apps.scan_server_view as SV
    monkeypatch.setattr(SV.ServerWatch, "is_local", lambda self: False)   # "the lab PC"
    fake = fake_service(16880, manifest=DEMO_MANIFEST)
    srv, win, c = rig()
    srv.instruments = {"magnet": {"host": "", "cmd": fake.cmd_port, "pub": fake.cmd_port + 1}}
    pump(qapp, lambda: win.registry.get("magnet.field") is not None, timeout=20)
    assert win.control._foreign == srv.pc                     # the lab's layouts
    assert not win.control.save_btn.isEnabled()
    assert f"watching {srv.pc}" in win.windowTitle()
    pid = next(p for p in win.control.items if p.startswith("magnet."))
    # the lab ticks a parameter on its Control tab -> this suite follows
    c.set_view({"plot": None, "panel": {"pids": [pid], "hidden": []}})
    pump(qapp, lambda: win.control.selected_pids() == [pid])
    # and back to this PC's own
    win.stop_watching()
    assert win.control._foreign is None and win.control.save_btn.isEnabled()
    assert win.registry.get("magnet.field") is None
    assert "watching" not in win.windowTitle()


def test_watching_another_pc_follows_its_scan_definition_and_navigator(
        qapp, rig, fake_service, monkeypatch, tmp_path):
    """Lukas 2026-10-06, the Navigator and Scan tabs side by side: the lab
    had S2.gds registered and a definition half-built; the office showed
    neither. The design FILE travels through the server (it exists only on
    the lab PC); the registration, selected point and the definition come
    with the view."""
    gdstk = pytest.importorskip("gdstk")
    from conftest import DEMO_MANIFEST
    import apps.scan_server_view as SV
    monkeypatch.setattr(SV.ServerWatch, "is_local", lambda self: False)
    fake = fake_service(16890, manifest=DEMO_MANIFEST)
    srv, win, c = rig()
    srv.instruments = {"magnet": {"host": "", "cmd": fake.cmd_port, "pub": fake.cmd_port + 1,
                                  "name": "Magnet field"}}
    pump(qapp, lambda: win.registry.get("magnet.field") is not None, timeout=20)
    assert win._group_names() == {"magnet": "Magnet field"}     # the lab's names

    lib = gdstk.Library()
    cell = lib.new_cell("CHIP")
    cell.add(gdstk.rectangle((-50, -50), (50, 50), layer=1))
    gds = tmp_path / "chip.gds"
    lib.write_gds(str(gds))
    # what the lab suite sends: the file, then its view
    assert c.set_design(gds, "gds", "CHIP")["ok"]
    reg = {"rotation_deg": 30.0, "mirror": False, "model": "auto", "shift": [0, 0],
           "points": [{"design": [0, 0], "stage": [100, 200]}]}
    definition = json.loads(Recipe(
        name="lab def", axes=[{"type": "linear", "param": "magnet.field",
                               "start": 0, "stop": 5, "num": 6}],
        detectors=[]).to_json())
    c.set_view({"plot": None, "panel": {"pids": [], "hidden": []}, "scan": definition,
                "nav": {"design": {"kind": "gds", "name": "chip.gds", "cell": "CHIP",
                                   "width_um": 0, "hidden_layers": []},
                        "registration": reg, "pick": [10.0, 5.0],
                        "fov_um": [80, 60], "approach_um": 2.0, "stage": ""}})
    nav = win.navigator
    pump(qapp, lambda: nav.design is not None and len(nav.reg.points) == 1, timeout=20)
    assert nav.reg.rotation_deg == 30.0 and nav.pick == (10.0, 5.0)
    assert (nav.fov_w.value(), nav.fov_h.value()) == (80, 60)
    pump(qapp, lambda: win.builder.name_edit.text() == "lab def")
    assert win.builder.build_recipe().axes[0]["num"] == 6
    # the shared file is the lab's bytes, not a path on the lab PC
    assert Path(nav.design.path).read_bytes() == gds.read_bytes()


def test_the_design_is_shared_by_the_servers_own_pc_only(server, client, tmp_path):
    from test_scan_server import OTHER_PC
    srv = server()
    c = client(srv)
    p = tmp_path / "x.gds"
    p.write_bytes(b"GDS")
    assert c.set_design(p)["ok"]
    assert c.get_design()["design"]["name"] == "x.gds"
    r = raw(srv, {"cmd": "set_design", "data": "AAAA", "client": OTHER_PC})
    assert not r["ok"] and r["refused"] == "not_this_pc"


# ─────────────── the lab's saved files, copied to the watcher ───────────────

def test_the_server_lists_its_files_and_hands_out_copies(server, client, tmp_path):
    """Lukas 2026-10-06: "view the actually measured file in the data viewer"
    from the office. Only .nc files under the data folder, in chunks."""
    from scan_core.scan_server import FILE_CHUNK
    srv = server()
    c = client(srv)
    c.submit(recipe(name="lab map", num=6))
    wait_for(lambda: srv._entries and srv._entries[0].result == "done")
    r = {}

    def listed():
        r.update(c.list_files())
        return any(f["name"] == "lab map" for f in r["files"])
    wait_for(listed)
    f = next(f for f in r["files"] if f["name"] == "lab map")
    assert not Path(f["path"]).is_absolute() and f["bytes"] > 0
    assert f["dims"] == ["field (6)"] and f["measured"]
    dest = c.download(f["path"], tmp_path / "copy" / "x.nc")
    original = next((tmp_path / "data").rglob("*lab_map*.nc"))
    assert dest.read_bytes() == original.read_bytes()
    assert FILE_CHUNK >= 2**20
    # nothing outside the data folder, nothing that is not a measurement
    outside = tmp_path / "outside.nc"
    outside.write_bytes(b"secret")
    for bad in ("../outside.nc", str(outside), "x.txt", "2026-01-01/none.nc"):
        rr = raw(srv, {"cmd": "get_file", "path": bad})
        assert not rr["ok"], bad


def test_a_big_file_comes_in_chunks(server, client, tmp_path, monkeypatch):
    from scan_core import scan_server as SS
    monkeypatch.setattr(SS, "FILE_CHUNK", 1000)
    srv = server()
    c = client(srv)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    blob = bytes(range(256)) * 20                      # 5120 bytes -> 6 chunks
    (tmp_path / "data" / "big.nc").write_bytes(blob)
    seen = []
    dest = c.download("big.nc", tmp_path / "big_copy.nc",
                      progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == blob
    assert len(seen) == 6 and seen[-1] == (5120, 5120)


def test_the_lab_files_dialog_opens_a_copy_in_the_data_tab(
        qapp, rig, fake_service, monkeypatch):
    from conftest import DEMO_MANIFEST
    import apps.scan_server_view as SV
    monkeypatch.setattr(SV.ServerWatch, "is_local", lambda self: False)
    fake = fake_service(16896, manifest=DEMO_MANIFEST)
    srv, win, c = rig()
    srv.instruments = {"magnet": {"host": "", "cmd": fake.cmd_port, "pub": fake.cmd_port + 1}}
    pump(qapp, lambda: not win.lab_files_btn.isHidden(), timeout=20)
    c.submit(recipe(name="office view", num=5))
    try:
        pump(qapp, lambda: srv._entries and srv._entries[0].result is not None, timeout=20)
    except AssertionError:
        print("SERVERLOG", "\n".join(srv._log[-15:]), srv.status_payload().get("state"))
        raise
    assert srv._entries[0].result == "done", srv._entries[0].error
    dlg = win.open_lab_files()
    try:
        pump(qapp, lambda: dlg.tree.topLevelItemCount() > 0
             and any(dlg.tree.topLevelItem(i).text(0) == "office view"
                     for i in range(dlg.tree.topLevelItemCount())), timeout=20)
    except AssertionError:
        raise AssertionError(dlg.status.text()) from None
    it = next(dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())
              if dlg.tree.topLevelItem(i).text(0) == "office view")
    dlg.open_item(it)
    pump(qapp, lambda: win.data_view.ds is not None and "lockin_r" in win.data_view.ds,
         timeout=20)
    assert "aaltoflow-lab-data" in str(win.data_view.path)
    dlg.close()
