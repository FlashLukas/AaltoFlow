"""The scan server, PHASE 2 (Lukas, 2026-10-10): define and submit scans from
ANOTHER PC, edit a running queue, copy a finished file to the watching PC.

What must hold, on the simulator with real sockets on scratch ports:

* the "same PC only" submit rule is gone; CONTROL decides: a client on
  another PC may submit while it holds control (or nobody does), and is
  refused while a different PC holds it -- and the other way round;
* the run info in the file is the SUBMITTING client's; the file is saved
  on the server's PC; get_scan names it relative to the data folder and
  get_file copies it;
* validation uses the SERVER's registry and live limits, not the client's;
* queue_add / queue_remove / queue_move change the running queue (only
  scans not started), are refused for the running scan and without
  control, log who did them, and move queue_rev so every watcher updates;
* Pause / Resume / Abort / Stop queue from a remote client;
* the watching suite: Run submits from another PC under the control rule,
  the RUN INFO card and the per-point box show only while it may submit,
  the queue card adds / removes / moves scans, and "Copy to this PC" opens
  a finished file in the Data tab.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

zmq = pytest.importorskip("zmq")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry                  # noqa: E402
from scan_core.scan_server_client import ScanServerClient, ScanServerError  # noqa: E402
from suite_common.control import ControlRefused                   # noqa: E402

from test_scan_server import (OTHER_PC, client, fast_polls, raw, recipe,  # noqa: E402,F401
                              server, slow_registry, wait_for)


@pytest.fixture
def office():
    """A ScanServerClient that says it runs on ANOTHER PC (anna@office-pc)."""
    made = []

    def make(srv):
        c = ScanServerClient("127.0.0.1", srv.cmd_port, srv.pub_port, timeout_ms=4000,
                             name="office suite")
        c.identity = dict(c.identity, host="anna@office-pc")
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def quick(name, num=3):
    """A short scan (no slow detector): runs in a fraction of a second."""
    return recipe(name=name, num=num, dets=("lockin_r",))


def long_scan(name="long"):
    return recipe(name=name, num=400, dets=("slow",))


def names(c):
    return [e["name"] for e in c.get_scan()["entries"]]


# ─────────────────────── submitting from another PC ────────────────────────

def test_another_pc_submits_with_control_and_its_run_info_goes_into_the_file(
        server, office, tmp_path):
    import xarray as xr
    srv = server()
    oc = office(srv)
    assert oc.take_control()
    r = oc.submit(quick("from office", 5), attrs={"sample": "S9", "operator": "anna"})
    assert r["accepted"]
    wait_for(lambda: not srv.running and srv._entries[0].result)
    st = oc.command("status")["status"]
    assert st["phase"] == 2 and "office-pc" in st["started_by"]
    e = oc.get_scan()["entries"][0]
    assert e["result"] == "done"
    # saved on the SERVER's PC (its data folder), with the office's run info
    path = Path(e["path"])
    assert path.is_file() and (tmp_path / "data") in path.parents
    with xr.open_dataset(path) as ds:
        assert ds.attrs["sample"] == "S9" and ds.attrs["operator"] == "anna"
    assert any("submitted by office suite (anna@office-pc)" in ln
               for ln in oc.get_log()["lines"])
    # nobody holding control: allowed too (the rule of every module)
    oc.release_control()
    assert oc.submit(quick("free"))["accepted"]
    wait_for(lambda: not srv.running)


def test_a_submit_is_refused_while_another_pc_holds_control(server, client, office):
    srv = server()
    lab = client(srv)                     # the suite on the server's own PC
    oc = office(srv)
    assert lab.take_control()
    with pytest.raises(ControlRefused):
        oc.submit(quick("office"))
    with pytest.raises(ControlRefused):
        oc.submit_queue([("a", quick("a")), ("b", quick("b"))])
    assert not srv.running
    # ... and the other way round: the office takes control, the lab PC is
    # now the one refused (control belongs to a PC, not to "the lab")
    assert oc.take_control(force=True)
    with pytest.raises(ControlRefused):
        lab.submit(quick("lab"))
    assert oc.submit(quick("office"))["accepted"]
    wait_for(lambda: not srv.running)


def test_validation_uses_the_servers_registry_and_live_limits(server, office):
    reg = slow_registry()
    reg.get("field").limits = (0.0, 20.0)       # the SERVER's magnet reaches 20 mT
    srv = server(registry=reg)
    oc = office(srv)
    # the office's own simulator would take 0..50 mT; the server must not
    assert build_sim_registry().get("field").limits[1] > 20.0
    with pytest.raises(ScanServerError) as info:
        oc.submit(quick("too far", 4))           # field 0 -> 50 mT
    assert info.value.refused == "invalid" and "field" in str(info.value)
    # a detector only the server has ("slow") is fine: its registry counts
    ok = Recipe(name="ok", axes=[{"type": "linear", "param": "field",
                                  "start": 0.0, "stop": 20.0, "num": 3}],
                detectors=["slow"])
    assert oc.submit(ok)["accepted"]
    wait_for(lambda: not srv.running)
    assert srv._entries[0].result == "done"


# ─────────────────────────── editing the queue ─────────────────────────────

def test_queue_add_remove_move_on_a_running_queue(server, office):
    srv = server()
    oc = office(srv)
    oc.submit_queue([("long", long_scan()), ("b", quick("b")), ("c", quick("c"))],
                    attrs={"sample": "S1"})
    wait_for(lambda: srv.status_payload()["done"] >= 1)
    rev0 = srv.status_payload()["queue_rev"]
    # add one at the end (with its own run info), one before "b"
    r = oc.queue_add(("d", quick("d")), attrs={"sample": "S2"})
    assert r["index"] == 3 and r["n"] == 4
    oc.queue_add([("x", quick("x"))], index=1)
    assert names(oc) == ["long", "x", "b", "c", "d"]
    assert srv.status_payload()["queue_rev"] > rev0
    ids = {e["name"]: e["id"] for e in oc.get_scan()["entries"]}
    # move "c" up before "x", remove "b"
    oc.queue_move(1, id=ids["c"])
    oc.queue_remove(id=ids["b"])
    assert names(oc) == ["long", "c", "x", "d"]
    entries = {e["name"]: e for e in oc.get_scan()["entries"]}
    assert entries["d"]["attrs"]["sample"] == "S2"
    assert "office-pc" in entries["d"]["added_by"]
    log = "\n".join(oc.get_log()["lines"])
    assert "QUEUE: 'd' added as scan 4 of 4 by office suite (anna@office-pc)" in log
    assert "QUEUE: 'c' moved from scan 4 to scan 2" in log
    assert "QUEUE: 'b' (scan 4) removed" in log
    # end the long one: the edited queue runs, in its new order
    oc.abort()
    wait_for(lambda: not srv.running, timeout=30)
    res = srv.status_payload()["queue"]["results"]
    assert [r_[:2] for r_ in res] == [["long", "aborted"], ["c", "done"], ["x", "done"],
                                      ["d", "done"]]


def test_queue_edits_are_refused_for_the_running_scan_and_without_control(
        server, client, office):
    srv = server()
    lab = client(srv)
    oc = office(srv)
    with pytest.raises(ScanServerError) as info:          # nothing runs
        oc.queue_add(("a", quick("a")))
    assert info.value.refused == "idle"
    oc.submit_queue([("long", long_scan()), ("b", quick("b"))])
    wait_for(lambda: srv.status_payload()["done"] >= 1)
    ids = {e["name"]: e["id"] for e in oc.get_scan()["entries"]}
    # the running scan: neither removed nor moved (Abort is for that)
    for call in (lambda: oc.queue_remove(id=ids["long"]),
                 lambda: oc.queue_move(1, id=ids["long"]),
                 lambda: oc.queue_move(0, id=ids["b"]),          # before the running one
                 lambda: oc.queue_add(("y", quick("y")), index=0)):
        with pytest.raises(ScanServerError) as info:
            call()
        assert info.value.refused == "started"
    with pytest.raises(ScanServerError) as info:
        oc.queue_remove(id=999999)
    assert info.value.refused == "unknown"
    # invalid for the SERVER's registry: not added
    bad = Recipe(name="bad", axes=[{"type": "linear", "param": "no_such_knob",
                                    "start": 0, "stop": 1, "num": 3}], detectors=["slow"])
    with pytest.raises(ScanServerError) as info:
        oc.queue_add(bad)
    assert info.value.refused == "invalid"
    # control: the lab PC takes it -> the office may not edit, but may Abort
    assert lab.take_control()
    for call in (lambda: oc.queue_add(("z", quick("z"))),
                 lambda: oc.queue_remove(id=ids["b"]),
                 lambda: oc.queue_move(1, id=ids["b"])):
        with pytest.raises(ControlRefused):
            call()
    assert names(oc) == ["long", "b"]
    lab.queue_remove(id=ids["b"])                          # the holder may
    assert names(oc) == ["long"]
    assert oc.stop_queue()["running"]
    wait_for(lambda: not srv.running, timeout=30)
    # a stopping / ended queue takes nothing more
    with pytest.raises(ScanServerError) as info:
        lab.queue_add(("late", quick("late")))
    assert info.value.refused == "idle"


def test_pause_resume_abort_and_stop_queue_from_another_pc(server, office):
    srv = server()
    oc = office(srv)
    assert oc.take_control()
    oc.submit_queue([("long", long_scan()), ("next", long_scan("next"))])
    wait_for(lambda: srv.status_payload()["done"] >= 2)
    assert oc.pause()["user_paused"]
    wait_for(lambda: srv.status_payload()["state"] == "paused")
    held = srv.status_payload()["done"]
    time.sleep(0.4)
    assert srv.status_payload()["done"] <= held + 1      # the point in progress ends
    assert not oc.resume()["user_paused"]
    wait_for(lambda: srv.status_payload()["done"] > held + 1)
    oc.abort()                                            # the next scan starts
    wait_for(lambda: srv.status_payload()["scan"] == "next")
    oc.stop_queue()
    wait_for(lambda: not srv.running, timeout=30)
    assert [r[:2] for r in srv.status_payload()["queue"]["results"]] == \
        [["long", "aborted"], ["next", "aborted"]]


# ─────────────────────────── a finished file ───────────────────────────────

def test_get_scan_names_the_finished_file_and_get_file_copies_it(server, office, tmp_path):
    srv = server()
    oc = office(srv)
    oc.submit_queue([("one", quick("one")), ("two", quick("two"))])
    wait_for(lambda: not srv.running)
    entries = oc.get_scan()["entries"]
    assert all(e["rel_path"] and not Path(e["rel_path"]).is_absolute() for e in entries)
    dest = oc.download(entries[1]["rel_path"], tmp_path / "office" / "two.nc")
    assert dest.read_bytes() == Path(entries[1]["path"]).read_bytes()
    assert not dest.with_name("two.nc.part").exists()


# ─────────────────────────── the watching suite ────────────────────────────

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from test_scan_server_suite import pump, qapp, rig                # noqa: E402,F401  (fixtures)


@pytest.fixture
def remote_rig(rig, monkeypatch):
    """A suite watching the server as if from ANOTHER PC."""
    import apps.scan_server_view as SV
    monkeypatch.setattr(SV.ServerWatch, "is_local", lambda self: False)
    return rig


def _visible(w, b):
    return w.isVisibleTo(b.right_pane)


def test_the_watcher_may_submit_under_control_and_shows_run_info_then(qapp, remote_rig):
    srv, win, c = remote_rig()
    b = win.builder
    # nobody holds control: this (remote) suite may submit
    pump(qapp, lambda: b.server_submit and b.run_btn.isEnabled())
    assert _visible(b.run_info, b) and _visible(b.per_pt, b) and _visible(b.name_edit, b)
    assert "Run starts scans" in win.server_ctrl_lbl.text()
    # another PC takes control: watch only -> run info and per-point hidden
    raw(srv, {"cmd": "take_control", "force": True, "client": OTHER_PC})
    pump(qapp, lambda: not b.server_submit)
    assert not b.run_btn.isEnabled()
    assert not _visible(b.run_info, b) and not _visible(b.per_pt, b)
    assert not _visible(b.per_pt_lbl, b) and not _visible(b.name_edit, b)
    assert "take control" in win.server_ctrl_lbl.text()
    b.load_recipe(Recipe(name="office run", axes=[{"type": "linear", "param": "field",
                                                   "start": 0.0, "stop": 10.0, "num": 4}],
                         detectors=["lockin_r"]))
    b.run_scan()                                    # refused here, nothing sent
    assert not srv.running and "take control" in b.detail.text()
    # take it over: may submit again
    ok, _ = win.watch.take_control(force=True)
    assert ok
    pump(qapp, lambda: b.server_submit and _visible(b.run_info, b))
    b.run_scan()
    pump(qapp, lambda: srv._entries and srv._entries[0].name == "office run"
         and not srv.running)
    assert srv._entries[0].result == "done" and b.worker is None


def test_the_watchers_run_info_goes_with_its_scan(qapp, remote_rig, monkeypatch):
    srv, win, c = remote_rig()
    b = win.builder
    pump(qapp, lambda: b.server_submit)
    monkeypatch.setattr(b.run_info, "attrs", lambda: {"sample": "S-office",
                                                     "operator": "anna"})
    b.load_recipe(Recipe(name="ri", axes=[{"type": "linear", "param": "field",
                                           "start": 0.0, "stop": 10.0, "num": 3}],
                         detectors=["lockin_r"]))
    b.run_scan()
    pump(qapp, lambda: srv._entries and not srv.running)
    assert srv._entries[0].attrs == {"sample": "S-office", "operator": "anna"}


def test_the_watchers_queue_card_adds_removes_and_moves(qapp, remote_rig):
    srv, win, c = remote_rig()
    b = win.builder
    c.submit_queue([("long", long_scan()), ("b", quick("b")), ("c", quick("c"))])
    pump(qapp, lambda: b.server_tree.topLevelItemCount() == 3
         and "running" in b.server_tree.topLevelItem(0).text(0))
    pump(qapp, lambda: b.queue_add_btn.isEnabled())
    # Add to queue: this Scan tab's definition goes to the end
    b.load_recipe(Recipe(name="added here", axes=[{"type": "linear", "param": "field",
                                                   "start": 0.0, "stop": 5.0, "num": 3}],
                         detectors=["lockin_r"]))
    assert b._queue_add_clicked()
    # queue_rev moved -> the watcher re-reads the queue by itself
    pump(qapp, lambda: b.server_tree.topLevelItemCount() == 4)
    assert b.server_tree.topLevelItem(3).text(0).startswith("4. added here")
    assert "added by" in b.server_tree.topLevelItem(3).text(0)
    # the running scan: no Remove / Up / Down
    b.server_tree.setCurrentItem(b.server_tree.topLevelItem(0))
    assert not b.queue_remove_btn.isEnabled() and not b.queue_down_btn.isEnabled()
    # "c" (3rd) up -> before "b"; it stays selected
    b.server_tree.setCurrentItem(b.server_tree.topLevelItem(2))
    assert b.queue_up_btn.isEnabled() and b.queue_remove_btn.isEnabled()
    assert b._queue_move_clicked(-1)
    pump(qapp, lambda: b.server_tree.topLevelItem(1).text(0).startswith("2. c"))
    pump(qapp, lambda: b._selected_server_entry(strict=True) == 1)
    assert not b.queue_up_btn.isEnabled()              # just after the running scan
    # remove "b" (now 3rd)
    b.server_tree.setCurrentItem(b.server_tree.topLevelItem(2))
    assert b._queue_remove_clicked()
    pump(qapp, lambda: b.server_tree.topLevelItemCount() == 3)
    assert [e.name for e in srv._entries] == ["long", "c", "added here"]
    # a queue loaded while the server is busy goes to the END of its queue
    from scan_core.scan_queue import QueueEntry
    assert b.run_queue([QueueEntry("q1", quick("q1")), QueueEntry("q2", quick("q2"))])
    pump(qapp, lambda: b.server_tree.topLevelItemCount() == 5)
    assert [e.name for e in srv._entries][-2:] == ["q1", "q2"]
    # another PC takes control: the edit buttons go away
    raw(srv, {"cmd": "take_control", "force": True, "client": OTHER_PC})
    pump(qapp, lambda: not b.queue_add_btn.isVisibleTo(b.right_pane))
    raw(srv, {"cmd": "stop_queue", "client": OTHER_PC})
    pump(qapp, lambda: not srv.running, timeout=30)


def test_copy_to_this_pc_opens_a_finished_scan_in_the_data_tab(qapp, remote_rig):
    srv, win, c = remote_rig()
    b = win.builder
    c.submit(quick("to copy", 4))
    pump(qapp, lambda: not srv.running and srv._entries[0].result == "done")
    pump(qapp, lambda: b.fetch_btn.isEnabled())       # the last saved scan
    b.server_tree.setCurrentItem(b.server_tree.topLevelItem(0))
    b.fetch_btn.click()
    pump(qapp, lambda: win.data_view.ds is not None and "lockin_r" in win.data_view.ds,
         timeout=20)
    assert "aaltoflow-lab-data" in str(win.data_view.path)
    assert Path(win.data_view.path).read_bytes() == Path(srv._entries[0].path).read_bytes()


def test_pause_resume_abort_from_the_remote_suite(qapp, remote_rig):
    srv, win, c = remote_rig()
    b = win.builder
    pump(qapp, lambda: b.server_submit)
    c.submit(long_scan())
    pump(qapp, lambda: b.pause_btn.isEnabled() and srv.status_payload()["done"] >= 1)
    b.pause_btn.click()
    pump(qapp, lambda: srv.status_payload()["user_paused"])
    pump(qapp, lambda: "Resume" in b.pause_btn.text() and b.pause_btn.isEnabled())
    b.pause_btn.click()
    pump(qapp, lambda: not srv.status_payload()["user_paused"])
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running, timeout=30)
    assert srv._entries[0].result == "aborted"


def test_the_local_suite_without_the_setting_hides_run_info(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    assert not b.server_submit
    assert not _visible(b.run_info, b) and not _visible(b.per_pt, b)
    win.run_on_server_box.setChecked(True)
    pump(qapp, lambda: b.server_submit)
    assert _visible(b.run_info, b) and _visible(b.per_pt, b)
    assert not _visible(b.server_box, b)            # the lab's own suite
    win.run_on_server_box.setChecked(False)
    assert not _visible(b.run_info, b) and _visible(b.server_box, b)
    win.stop_watching()
    assert _visible(b.run_info, b) and _visible(b.per_pt, b)
