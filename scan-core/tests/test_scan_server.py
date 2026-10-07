"""The SCAN SERVER (scan_core/scan_server.py): scans in a service of their own.

Phase 1 = WATCH (Lukas, 2026-10-05: "set a measurement on a lab pc but then
observe/set on my office pc"). What must hold, each tested here on the
simulator with real sockets on scratch ports:

* a scan submitted from THIS PC runs, reports progress, and is saved exactly
  like the suite saves it (dated file, run info attributes);
* a client on ANOTHER PC may watch (status, live data, log) but not submit
  -- the refusal says "phase 2";
* get_live hands over the engine's partial snapshot (unmeasured points NaN);
* Abort / Stop queue / the operator pause (Continue, Abort, Abort all) work,
  in a single scan and in a queue;
* control: a viewer on another PC cannot answer the pause, but CAN Abort (a
  safety verb);
* a client that disappears mid-scan changes nothing;
* shutdown while a scan runs aborts it, saves, and exits;
* encryption: with the lab's policy securing "scanserver", a keyed client
  works and a plain one gets no answer.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import time
from pathlib import Path

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, hooks          # noqa: E402
from scan_core.registry import Gettable                          # noqa: E402
from scan_core.scan_server import (ScanServer, dataset_from_text,  # noqa: E402
                                   dataset_to_text, where_text)
from scan_core.scan_server_client import ScanServerClient, ScanServerError  # noqa: E402
from suite_common.control import ControlRefused                   # noqa: E402

OTHER_PC = {"id": "office-1", "kind": "gui", "name": "office suite",
            "host": "anna@office-pc"}


def free_ports() -> tuple[int, int]:
    """Two free neighbouring ports outside the ephemeral range."""
    while True:
        cmd = random.randrange(20000, 40000)
        with socket.socket() as a, socket.socket() as b:
            try:
                a.bind(("127.0.0.1", cmd)); b.bind(("127.0.0.1", cmd + 1))
                return cmd, cmd + 1
            except OSError:
                continue


def slow_registry(dwell: float = 0.03):
    """The simulator plus a detector that takes `dwell` seconds per reading,
    so a scan lasts long enough to be watched."""
    reg = build_sim_registry()

    def slow():
        time.sleep(dwell)
        return 1.0
    reg.add(Gettable("slow", "Slow detector", "V", slow))
    return reg


def recipe(name="map", num=20, hooks_=(), dets=("lockin_r", "slow")):
    return Recipe(name=name, axes=[{"type": "linear", "param": "field",
                                    "start": 0.0, "stop": 50.0, "num": num}],
                  detectors=list(dets), hooks=list(hooks_))


def pause_step(message="Insert the polariser"):
    return {"when": "before_scan", "action": "call",
            "args": {"steps": [{"pause": {"message": message}}]}}


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(hooks, "PAUSE_POLL_S", 0.01)


@pytest.fixture
def server(tmp_path):
    made = []

    def make(registry=None, **kw):
        cmd, pub = free_ports()
        srv = ScanServer(host="127.0.0.1", cmd_port=cmd, pub_port=pub,
                         registry=registry or slow_registry(), data_dir=tmp_path / "data",
                         echo=False, live_every_s=0.2, status_hz=10.0, **kw)
        srv.start()
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.stop()


@pytest.fixture
def client():
    made = []

    def make(srv, **kw):
        c = ScanServerClient("127.0.0.1", srv.cmd_port, srv.pub_port, timeout_ms=4000, **kw)
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def raw(srv, req: dict, timeout_ms: int = 3000) -> dict:
    """One request with a hand-made identity (another PC, say)."""
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0); s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    s.connect(f"tcp://127.0.0.1:{srv.cmd_port}")
    try:
        s.send_json(req)
        return s.recv_json()
    finally:
        s.close(0)


def wait_for(pred, timeout=15.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


# ───────────────────────────── submit and watch ───────────────────────────────

def test_submit_from_this_pc_runs_reports_progress_and_saves(server, client, tmp_path):
    import xarray as xr
    srv = server()
    c = client(srv)
    r = c.submit(recipe(num=40), attrs={"sample": "S1", "operator": "lf"})
    assert r["accepted"] and r["names"] == ["map"]
    # mid-scan: running, some points done, where + ETA in the status
    st = wait_for(lambda: (lambda s: s if 0 < s["done"] < s["total"] else None)(
        c.command("status")["status"]))
    assert st["state"] == "running" and st["total"] == 40 and st["scan"] == "map"
    assert "point" in st["where"] and "field" in st["where"]
    assert st["eta_s"] is not None and st["save_path"].endswith("_map.nc")
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    st = c.command("status")["status"]
    assert st["done"] == 40 and st["queue"]["results"][0][1] == "done"
    path = Path(st["last_saved"])
    assert path.is_file() and path.parent.parent == tmp_path / "data"
    with xr.open_dataset(path) as ds:
        assert ds.attrs["sample"] == "S1" and ds.attrs["operator"] == "lf"
        assert ds.attrs["name"] == "map"
        assert np.isfinite(ds["lockin_r"].values).all()
    lines = c.get_log()["lines"]
    assert any("submitted by" in ln for ln in lines)
    assert any("saved to" in ln for ln in lines)


def test_another_pc_may_watch_but_not_submit(server, client):
    srv = server()
    r = raw(srv, {"cmd": "submit", "recipe": json.loads(recipe().to_json()),
                  "client": OTHER_PC})
    assert not r["ok"] and r["refused"] == "phase2"
    assert "phase 2" in r["error"] and "office-pc" in r["error"]
    # no identity at all: refused too (a scan is accepted only from this PC)
    r = raw(srv, {"cmd": "submit", "recipe": json.loads(recipe().to_json())})
    assert not r["ok"] and r["refused"] == "phase2"
    # ... watching works from anywhere
    c = client(srv)
    c.submit(recipe(num=30))
    wait_for(lambda: c.command("status")["status"]["done"] > 2)
    st = raw(srv, {"cmd": "status", "client": OTHER_PC})
    assert st["ok"] and st["status"]["state"] in ("running", "idle")
    live = raw(srv, {"cmd": "get_live", "have_rev": -1, "client": OTHER_PC}, 10000)
    assert live["ok"] and live["data"]
    log = raw(srv, {"cmd": "get_log", "since": 0, "client": OTHER_PC})
    assert log["ok"] and log["lines"]


def test_a_second_submit_is_refused_while_a_scan_runs(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=60))
    with pytest.raises(ScanServerError) as info:
        c.submit(recipe(name="second"))
    assert info.value.refused == "busy"
    c.abort()


def test_an_invalid_recipe_is_refused_before_anything_runs(server, client):
    srv = server()
    c = client(srv)
    bad = Recipe(name="bad", axes=[{"type": "linear", "param": "no_such_knob",
                                    "start": 0, "stop": 1, "num": 3}], detectors=["slow"])
    with pytest.raises(ScanServerError) as info:
        c.submit(bad)
    assert info.value.refused == "invalid" and "no_such_knob" in str(info.value)
    assert c.command("status")["status"]["state"] == "idle"


# ─────────────────────────────── live data ────────────────────────────────────

def test_get_live_is_the_engines_partial_snapshot(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=60, dets=("slow",)))
    wait_for(lambda: c.command("status")["status"]["live_rev"] >= 2)
    ds = c.get_live()
    assert ds is not None and "slow" in ds and ds.sizes["field"] == 60
    vals = ds["slow"].values
    # measured points are 1.0, the rest not measured yet (NaN)
    assert np.isnan(vals).any() and (vals[np.isfinite(vals)] == 1.0).all()
    # the same revision again: nothing is sent
    assert c.get_live() is None or c.live_rev > 0
    r = c.command("get_live", have_rev=c.live_rev)
    assert r.get("unchanged") or r["live_rev"] > c.live_rev
    c.abort()
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    final = c.get_live(force=True)
    assert final.attrs["name"] == "map"
    # the wire format round-trips a dataset unchanged
    back = dataset_from_text(dataset_to_text(final))
    np.testing.assert_array_equal(back["slow"].values, final["slow"].values)


# ──────────────────────── abort, stop queue, pause ────────────────────────────

def test_abort_keeps_the_points_and_saves(server, client):
    import xarray as xr
    srv = server()
    c = client(srv)
    c.submit(recipe(num=200, dets=("slow",)))
    wait_for(lambda: c.command("status")["status"]["done"] >= 5)
    assert c.abort()["running"]
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    st = c.command("status")["status"]
    assert st["queue"]["results"][0][1] == "aborted"
    with xr.open_dataset(st["last_saved"]) as ds:
        n = int(np.isfinite(ds["slow"].values).sum())
    assert 5 <= n < 200


def test_queue_abort_skips_to_the_next_and_stop_queue_ends_it(server, client):
    srv = server()
    c = client(srv)
    c.submit_queue([("one", recipe(num=200, dets=("slow",))),
                    ("two", recipe(num=5)), ("three", recipe(num=5))])
    st = wait_for(lambda: (lambda s: s if s["done"] >= 3 else None)(
        c.command("status")["status"]))
    assert st["queue"]["n"] == 3 and st["queue"]["pos"] == 1 and "scan 1 of 3" in st["where"]
    c.abort()                                           # skips "one"
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    res = [r[:2] for r in c.command("status")["status"]["queue"]["results"]]
    assert res == [["one", "aborted"], ["two", "done"], ["three", "done"]]

    c.submit_queue([("a", recipe(num=200, dets=("slow",))), ("b", recipe(num=5))])
    wait_for(lambda: c.command("status")["status"]["done"] >= 3)
    c.stop_queue()
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    q = c.command("status")["status"]["queue"]
    assert [r[:2] for r in q["results"]] == [["a", "aborted"]]
    assert "1 not run" in q["summary"] and "stopped by" in q["summary"]


def test_operator_pause_continue_and_abort_all(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=5, hooks_=[pause_step("Rotate the sample")]))
    st = wait_for(lambda: (lambda s: s if s["state"] == "waiting_operator" else None)(
        c.command("status")["status"]))
    assert st["pause_message"] == "Rotate the sample"
    c.answer_pause(True)
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    assert c.command("status")["status"]["queue"]["results"][0][1] == "done"
    with pytest.raises(ScanServerError):
        c.answer_pause(True)                            # nothing is asked now

    # Abort ALL at a pause in a queue: this scan stops AND the queue ends
    c.submit_queue([("p", recipe(num=5, hooks_=[pause_step()])), ("next", recipe(num=5))])
    wait_for(lambda: c.command("status")["status"]["state"] == "waiting_operator")
    c.answer_pause("all")
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    q = c.command("status")["status"]["queue"]
    assert [r[:2] for r in q["results"]] == [["p", "aborted"]]
    assert "abort all" in q["summary"]


# ─────────────────────────────── control ──────────────────────────────────────

def test_a_viewer_cannot_answer_the_pause_but_can_abort(server, client):
    srv = server()
    lab_gui = client(srv)
    assert lab_gui.take_control()                       # the lab PC holds control
    lab_gui.submit(recipe(num=5, hooks_=[pause_step()]))
    wait_for(lambda: lab_gui.command("status")["status"]["state"] == "waiting_operator")
    r = raw(srv, {"cmd": "answer_pause", "answer": True, "client": OTHER_PC})
    assert not r["ok"] and r["refused"] == "control"
    assert srv.status_payload()["state"] == "waiting_operator"
    # Abort is a SAFETY verb: always allowed, also for a viewer on another PC
    r = raw(srv, {"cmd": "abort", "client": OTHER_PC})
    assert r["ok"] and r["running"]
    wait_for(lambda: lab_gui.command("status")["status"]["state"] == "idle")
    assert lab_gui.command("status")["status"]["queue"]["results"][0][1] == "aborted"


def test_the_holder_on_another_pc_does_not_lock_out_this_pcs_abort(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=200, dets=("slow",)))
    # the office takes control (forced) -- this PC is now a viewer
    r = raw(srv, {"cmd": "take_control", "force": True, "client": OTHER_PC})
    assert r["ok"] and r["granted"]
    with pytest.raises(ControlRefused):
        c.answer_pause(True)
    assert c.abort()["running"]                         # ... but Abort always works
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")


# ─────────────────────────────── robustness ───────────────────────────────────

def test_a_client_that_disappears_mid_scan_changes_nothing(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=60, dets=("slow",)))
    wait_for(lambda: c.command("status")["status"]["done"] >= 3)
    c.close()                                           # the suite window closed
    wait_for(lambda: not srv.running, timeout=20)
    st = srv.status_payload()
    assert st["done"] == 60 and st["queue"]["results"][0][1] == "done"


def test_shutdown_while_running_aborts_saves_and_stops(server, client):
    srv = server()
    c = client(srv)
    c.submit(recipe(num=300, dets=("slow",)))
    wait_for(lambda: c.command("status")["status"]["done"] >= 3)
    r = c.command("shutdown")
    assert r["stopping"] and r["aborting"] == "map"
    wait_for(lambda: srv._stop.is_set(), timeout=20)
    res = srv._entries[0]
    assert res.result == "aborted" and Path(res.path).is_file()


def test_status_works_with_no_client_and_describe_is_honest(server):
    srv = server()
    st = srv.status_payload()
    assert st["state"] == "idle" and st["phase"] == 1 and "control" in st
    d = raw(srv, {"cmd": "describe"})["describe"]
    assert d["module"] == "scanserver"
    ids = {p["id"] for p in d["parameters"]}
    assert {"state", "progress", "abort", "stop_queue", "answer_pause"} <= ids
    assert "abort" in st["control"]["always"] and "stop_queue" in st["control"]["always"]


def test_a_fault_pauses_and_clear_fault_goes_to_the_module(server, client):
    reg = slow_registry()
    faults = [("camera", "pattern lost")]
    reg.fault_check = lambda ids=None: list(faults)

    class FakeLab:
        cleared = []

        def set_abort(self, fn):
            pass

        def can_clear_fault(self, name):
            return name == "camera"

        def clear_fault(self, name):
            self.cleared.append(name)
            faults.clear()
            return {"ok": True}

        def close(self):
            pass

    srv = server(registry=reg)
    srv.lab = FakeLab()
    c = client(srv)
    c.submit(recipe(num=5))
    st = wait_for(lambda: (lambda s: s if s["state"] == "paused" else None)(
        c.command("status")["status"]))
    assert st["faults"] == [{"module": "camera", "message": "pattern lost",
                             "can_clear": True}]
    c.clear_fault("camera")
    assert FakeLab.cleared == ["camera"]
    wait_for(lambda: c.command("status")["status"]["state"] == "idle", timeout=20)
    assert c.command("status")["status"]["queue"]["results"][0][1] == "done"


# ──────────────────────── the operator's Pause / Resume ───────────────────────

def test_pause_holds_the_scan_and_resume_finishes_it(server, client):
    srv = server()
    c = client(srv)
    st = c.command("status")["status"]
    assert st["user_paused"] is False
    assert c.pause()["running"] is False                # nothing runs: harmless
    c.submit(recipe(num=60, dets=("slow",)))
    wait_for(lambda: c.command("status")["status"]["done"] >= 3)
    r = c.pause()
    assert r["running"] and r["user_paused"]
    st = wait_for(lambda: (lambda s: s if s["user_paused"] and s["state"] == "paused"
                           else None)(c.command("status")["status"]))
    assert st["faults"] == []                           # not a FAULT pause
    # the point in progress finishes, then nothing more is measured
    wait_for(lambda: any("PAUSED by the operator" in ln for ln in c.get_log()["lines"]))
    n = c.command("status")["status"]["done"]
    time.sleep(0.5)
    assert c.command("status")["status"]["done"] == n < 60
    r = c.resume()
    assert r["running"] and not r["user_paused"]
    wait_for(lambda: c.command("status")["status"]["state"] == "idle", timeout=20)
    st = c.command("status")["status"]
    assert st["done"] == 60 and st["queue"]["results"][0][1] == "done"
    assert st["user_paused"] is False
    assert any(ln.endswith("resumed") for ln in c.get_log()["lines"])


def test_abort_while_paused_and_the_next_scan_of_a_queue_starts_unpaused(server, client):
    srv = server()
    c = client(srv)
    c.submit_queue([("one", recipe(num=200, dets=("slow",))), ("two", recipe(num=5))])
    wait_for(lambda: c.command("status")["status"]["done"] >= 3)
    c.pause()
    wait_for(lambda: any("PAUSED by the operator" in ln for ln in c.get_log()["lines"]))
    c.abort()                                           # skips "one", as always
    wait_for(lambda: c.command("status")["status"]["state"] == "idle", timeout=20)
    q = c.command("status")["status"]["queue"]
    # "two" was not held: the pause belonged to "one" only
    assert [r[:2] for r in q["results"]] == [["one", "aborted"], ["two", "done"]]


def test_pause_is_a_safety_verb_and_resume_needs_control(server, client):
    srv = server()
    lab_gui = client(srv)
    assert lab_gui.take_control()                       # the lab PC holds control
    lab_gui.submit(recipe(num=200, dets=("slow",)))
    wait_for(lambda: lab_gui.command("status")["status"]["done"] >= 2)
    # a viewer on another PC may PAUSE (like abort) ...
    r = raw(srv, {"cmd": "pause", "client": OTHER_PC})
    assert r["ok"] and r["user_paused"]
    assert srv.status_payload()["user_paused"]
    # ... but not RESUME: carrying on is the controller's decision
    r = raw(srv, {"cmd": "resume", "client": OTHER_PC})
    assert not r["ok"] and r["refused"] == "control"
    assert srv.status_payload()["user_paused"]
    assert lab_gui.resume()["running"]                  # the holder may
    assert not srv.status_payload()["user_paused"]
    lab_gui.abort()
    wait_for(lambda: lab_gui.command("status")["status"]["state"] == "idle")
    st = srv.status_payload()
    assert "pause" in st["control"]["always"] and "resume" not in st["control"]["always"]
    ids = {p["id"]: p for p in raw(srv, {"cmd": "describe"})["describe"]["parameters"]}
    assert ids["pause"]["order"] == 89 and ids["pause"]["kind"] == "action"
    assert "resume" in ids and "user_paused" in ids


def test_where_text_matches_the_suites_line():
    where = {"row": None, "axes": [
        {"name": "field", "i": 4, "n": 9, "value": 40.0, "unit": "mT"}]}
    t = where_text(where, 25, 125, 200.0, queue_i=1, queue_n=3, now="set x (before_scan)")
    assert t == ("scan 2 of 3   point 25 / 125   field 40 mT (5/9)   ~3m 20s left   "
                 "now: set x (before_scan)")


# ─────────────────────────────── encryption ───────────────────────────────────

def test_encrypted_when_the_policy_secures_it(tmp_path, monkeypatch, server, client):
    """A throw-away keyring (as tools/check_modules.py makes one): this PC's
    key, the keyring holding it, policy enforce ["*"]."""
    from suite_common import secure as S
    me = tmp_path / "pc"
    kr = me / "keyring"
    kr.mkdir(parents=True)
    pub, sec = S.new_keypair()
    meta = {"pc": socket.gethostname().lower(), "machine": "yes"}
    S.write_cert(me / S.OWN_PUBLIC, pub, meta=meta)
    S.write_cert(me / S.OWN_SECRET, pub, sec, meta=meta)
    S.write_cert(kr / "this.key", pub, meta=meta)
    (me / S.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}), encoding="utf-8")
    (kr / S.POLICY_FILE).write_text(json.dumps({"mode": "enforce", "modules": ["*"]}),
                                    encoding="utf-8")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(me))
    srv = server()
    assert srv._guard is not None
    with pytest.raises(Exception):
        raw(srv, {"cmd": "status"}, timeout_ms=800)     # a plain client: no answer
    c = client(srv)                                     # CurveZMQ with this PC's key
    c.submit(recipe(num=5))
    wait_for(lambda: c.command("status")["status"]["state"] == "idle")
    assert c.command("status")["status"]["queue"]["results"][0][1] == "done"


# ──────────────────────── following the launcher ──────────────────────────────

def _module(root: Path, folder: str, key: str, cmd: int):
    d = root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    (d / "module.toml").write_text(
        f'[module]\nkey = "{key}"\nname = "{key} module"\n'
        f'[ports]\ncmd = {cmd}\npub = {cmd + 1}\n'
        f'[run]\nservice = "scripts/run_service.py"\ngui = ""\n')


def test_the_server_follows_the_launcher_and_skips_itself(tmp_path, fake_service, client):
    """With no fixed registry the server connects the INSTRUMENT modules
    running on its PC -- never a scan server (itself included)."""
    from conftest import DEMO_MANIFEST
    cmd, _pub = free_ports()
    svc = fake_service(cmd, manifest=DEMO_MANIFEST, adopt_delay=0.02, settle_delay=0.02)
    _module(tmp_path, "modules/field/magnet-control", "magnet", svc.cmd_port)
    s_cmd, s_pub = free_ports()
    _module(tmp_path, "scan-core", "scanserver", s_cmd)
    srv = ScanServer(host="127.0.0.1", cmd_port=s_cmd, pub_port=s_pub, root=tmp_path,
                     data_dir=tmp_path / "data", echo=False, follow_every_s=0.2)
    srv.start()
    try:
        wait_for(lambda: srv.connected == ["magnet"])
        assert srv.registry.get("magnet.field") is not None
        assert not any(p.id.startswith("scanserver.") for p in srv.registry.settables())
        c = client(srv)
        c.submit(Recipe(name="lab", axes=[{"type": "array", "param": "magnet.field",
                                           "values": [1.0, 2.0, 3.0]}],
                        detectors=["magnet.measured_field"]))
        wait_for(lambda: not srv.running and srv._entries[0].result is not None)
        assert srv._entries[0].result == "done", srv._entries[0].error
        assert svc.commands.count("set_field") == 3
        st = c.command("status")["status"]
        assert st["modules"] == ["magnet"]
    finally:
        srv.stop()


def test_a_module_port_answered_by_the_scan_server_is_refused(server):
    """Found on the lab PC 2026-10-05: the scan server, moved onto sr830's
    default ports, was followed as 'sr830' (14 phantom detectors). A service
    whose describe names another module must not be connected."""
    from scan_core.lab import build_lab_registry
    srv = server()
    with pytest.raises(RuntimeError, match="scanserver"):
        build_lab_registry(include=("sr830",), prefix=True,
                           endpoints={"sr830": ("127.0.0.1", srv.cmd_port, srv.pub_port)})


def test_identity_accepts_the_key_and_its_discovery_slugs():
    from scan_core.lab import _check_identity
    for name in ("hf2", "HF2", "hf2_labpc", "hf2_labpc_5569"):
        _check_identity(name, {"module": "hf2"})
    _check_identity("anything", {})                 # no module named: nothing to check
    _check_identity("clMag", {"module": "fake"})    # unknown to the suite: allowed
    with pytest.raises(RuntimeError):
        _check_identity("clMag", {"module": "scanserver"})
