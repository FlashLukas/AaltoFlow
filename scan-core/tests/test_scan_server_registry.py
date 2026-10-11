"""The scan server's REGISTRY for watchers (2026-10-11, Lukas: yes).

A suite watching a scan server defines scans for the SERVER's instruments. The
server answers `get_registry` with its parameter manifest
(scan_core/registry_wire.py) and puts `registry_rev` into every status frame;
the watching suite builds its Scan tab on a PROXY registry made from it. What
must hold, offline, with real sockets on scratch ports:

* the manifest is plain JSON and a proxy built from it has the same
  parameters, limits, units and capabilities as the original -- and refuses
  to move anything (the scan runs on the server);
* get_registry works on a server with a FIXED registry (--sim), the gap it
  closes; it is a read verb (allowed while another PC holds control);
* registry_rev moves when the registry changes, and only then; a proxy is
  updated in place when only values moved;
* a proxy validates a recipe like the server does: in-limit accepted,
  out-of-limit refused;
* the watching suite's Scan tab uses the proxy (the server's detectors and
  limits, not its own simulator's), keeps the axis stack when the lab's
  registry changes, and falls back to the old behaviour on an older server.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

zmq = pytest.importorskip("zmq")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry                  # noqa: E402
from scan_core.registry import Action, Gettable                   # noqa: E402
from scan_core.registry_wire import (RemoteOnly, registry_from_wire,  # noqa: E402
                                     registry_to_wire, update_proxy, wire_revision)
from scan_core.scan_server import ScanServer                      # noqa: E402
from scan_core.scan_server_client import ScanServerError          # noqa: E402

from test_scan_server import (OTHER_PC, client, fast_polls, raw, server,  # noqa: E402,F401
                              slow_registry, wait_for)


def field_scan(stop, dets=("lockin_r",), num=3):
    return Recipe(name="f", axes=[{"type": "linear", "param": "field",
                                   "start": 0.0, "stop": float(stop), "num": num}],
                  detectors=list(dets))


# ───────────────────────────── the wire format ──────────────────────────────

def test_the_manifest_is_json_and_the_proxy_matches_the_original():
    reg = build_sim_registry()
    reg.add_action(Action("sim.ref", "Take reference", lambda args=None: None,
                          arg_specs=[{"name": "n", "type": "int", "default": 4,
                                      "min": 1, "max": 64}]))
    reg.get_action("sim.ref").wait = {"policy": "flag_only", "key": "busy"}
    wire = registry_to_wire(reg)
    text = json.dumps(wire, allow_nan=False)            # strict JSON: no inf / nan
    proxy = registry_from_wire(json.loads(text))
    assert [p.id for p in proxy.settables()] == [p.id for p in reg.settables()]
    assert [p.id for p in proxy.gettables()] == [p.id for p in reg.gettables()]
    assert [a.id for a in proxy.actions()] == [a.id for a in reg.actions()]
    for p in reg.settables():
        q = proxy.get(p.id)
        assert (q.label, q.unit) == (p.label, p.unit)
        lo, hi = p.limits
        assert q.limits == (float(lo), float(hi))        # inf travels as None and back
        assert (q.stream is None) == (getattr(p, "stream", None) is None)
        assert (q.ramp is None) == (getattr(p, "ramp", None) is None)
        if p.ramp is not None:
            assert q.ramp.rate_limits == p.ramp.rate_limits
            assert q.ramp.rate_default == p.ramp.rate_default
            assert q.ramp.binned_by == p.ramp.binned_by
    for g in reg.gettables():
        q = proxy.get(g.id)
        assert q.dtype == g.dtype and len(q.axes) == len(g.axes)
        assert (q.acquire is None) == (g.acquire is None)
        if g.acquire is not None:
            assert q.acquire.group == g.acquire.group
        assert q.storage.kind == g.storage.kind
    a = proxy.get_action("sim.ref")
    assert a.arg_specs[0]["max"] == 64 and a.takes_args and a.wait["key"] == "busy"
    # it moves nothing: the scan runs on the server
    with pytest.raises(RemoteOnly):
        proxy.get("field").set(1.0)
    with pytest.raises(RemoteOnly):
        proxy.get("lockin_r").get()
    with pytest.raises(RemoteOnly):
        a.run()


def test_a_proxy_validates_like_the_original():
    reg = build_sim_registry()
    reg.get("field").limits = (0.0, 20.0)
    proxy = registry_from_wire(registry_to_wire(reg))
    assert field_scan(20).validate(proxy) == []
    errs = field_scan(50).validate(proxy)
    assert errs and errs == field_scan(50).validate(reg)
    assert any("field" in e for e in errs)
    # unknown parameters, too
    assert field_scan(10, dets=("nope",)).validate(proxy)
    # compile works (the ETA and the summary use it)
    assert field_scan(20, num=7).compile(proxy).n_points == 7


def test_a_proxy_reads_through_a_direct_connection_when_given_one():
    reg = build_sim_registry()
    proxy = registry_from_wire(registry_to_wire(reg), read_from=lambda: reg)
    reg.get("field").set(12.0)
    assert proxy.get("field").get() == pytest.approx(reg.get("field").get())
    with pytest.raises(RemoteOnly):
        proxy.get("field").set(3.0)                    # reading yes, setting never


def test_update_proxy_in_place_only_when_the_parameters_are_the_same():
    reg = build_sim_registry()
    proxy = registry_from_wire(registry_to_wire(reg))
    field = proxy.get("field")
    reg.get("field").limits = (-5.0, 5.0)
    w1 = registry_to_wire(reg)
    assert update_proxy(proxy, w1)
    assert proxy.get("field") is field and field.limits == (-5.0, 5.0)
    reg.add(Gettable("extra", "Extra", "V", lambda: 0.0))
    assert not update_proxy(proxy, registry_to_wire(reg))   # came or went: rebuild
    assert wire_revision(w1) != wire_revision(registry_to_wire(reg))
    assert wire_revision(w1) == wire_revision(json.loads(json.dumps(w1)))


# ───────────────────────────── the server verb ──────────────────────────────

def test_get_registry_on_a_server_with_a_fixed_registry(server, client):
    reg = slow_registry()
    reg.get("field").limits = (0.0, 20.0)             # the SERVER's magnet
    srv = server(registry=reg)
    c = client(srv)
    st = c.command("status")["status"]
    r = c.get_registry()
    assert r["registry_rev"] == st["registry_rev"] != 0
    assert r["follow"] is False
    proxy = registry_from_wire(r["registry"])
    assert proxy.get("slow") is not None              # a detector only the server has
    assert proxy.get("field").limits == (0.0, 20.0)
    assert field_scan(20, dets=("slow",)).validate(proxy) == []
    assert field_scan(50).validate(proxy)              # the server would refuse it too
    with pytest.raises(ScanServerError):
        c.submit(field_scan(50))


def test_get_registry_is_a_read_verb(server, client):
    srv = server()
    lab = client(srv)
    assert lab.take_control()
    # another PC, while the lab holds control: watching is free
    r = raw(srv, {"cmd": "get_registry", "client": OTHER_PC})
    assert r["ok"] and r["registry"]["settables"]


def test_registry_rev_moves_when_the_registry_changes(server, client):
    reg = slow_registry()
    srv = server(registry=reg)
    c = client(srv)
    rev0 = c.get_registry()["registry_rev"]
    assert c.get_registry()["registry_rev"] == rev0    # nothing changed: same number
    reg.get("field").limits = (0.0, 7.0)               # a limit moved on the lab
    rev1 = c.get_registry()["registry_rev"]
    assert rev1 != rev0
    wait_for(lambda: c.command("status")["status"]["registry_rev"] == rev1)
    reg.add(Gettable("new_det", "New", "V", lambda: 1.0))   # a module came
    srv.registry_manifest()                           # (the follower does this every 3 s)
    st = c.command("status")["status"]
    assert st["registry_rev"] not in (rev0, rev1)
    assert registry_from_wire(c.get_registry()["registry"]).get("new_det") is not None


# ───────────────────────────── the watching suite ───────────────────────────

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


def test_the_watchers_scan_tab_builds_on_the_servers_registry(qapp, remote_rig):
    reg = slow_registry()
    reg.get("field").limits = (0.0, 20.0)
    srv, win, c = remote_rig(registry=reg)
    b = win.builder
    pump(qapp, lambda: win._proxy is not None and b.registry is win._proxy)
    # the server's parameters and limits -- not this PC's simulator's
    assert b.registry.get("slow") is not None and win.registry.get("slow") is None
    assert b.registry.get("field").limits == (0.0, 20.0)
    assert field_scan(50).validate(b.registry)
    assert field_scan(20, dets=("slow",)).validate(b.registry) == []
    # an axis on the Scan tab ...
    b.load_recipe(field_scan(20, dets=("slow",), num=4))
    assert len(b.rows) == 1
    # ... survives a limit moving on the lab (updated in place, re-clamped)
    proxy = win._proxy
    reg.get("field").limits = (0.0, 10.0)
    srv.registry_manifest()
    pump(qapp, lambda: b.registry.get("field").limits == (0.0, 10.0))
    assert win._proxy is proxy and len(b.rows) == 1
    # ... and a module coming on the lab (a new proxy, the definition kept)
    reg.add(Gettable("new_det", "New", "V", lambda: 1.0))
    srv.registry_manifest()
    pump(qapp, lambda: b.registry.get("new_det") is not None)
    assert win._proxy is not proxy and len(b.rows) == 1
    assert b.rows[0].param is b.registry.get("field")
    # Run: the server runs it (validated there too)
    pump(qapp, lambda: b.server_submit)
    b.run_scan()
    pump(qapp, lambda: srv._entries and not srv.running)
    assert srv._entries[0].result == "done"
    # Stop watching: this PC's own registry again
    win.stop_watching()
    assert win._proxy is None and b.registry is win.registry


def test_the_suite_on_the_servers_pc_uses_its_registry_too(qapp, rig):
    srv, win, c = rig(settings={"run_on_scan_server": True})
    b = win.builder
    pump(qapp, lambda: b.registry is win._proxy and win._proxy is not None)
    assert b.registry.get("slow") is not None
    # the queue card with its edit buttons (it may submit there)
    assert b.server_box.isVisibleTo(b.right_pane)
    assert b.queue_add_btn.isVisibleTo(b.right_pane)
    win.stop_watching()
    assert b.registry is win.registry


def test_an_older_server_without_the_verb_keeps_the_old_behaviour(qapp, remote_rig,
                                                                  monkeypatch):
    plain_status = ScanServer.status_payload
    plain_dispatch = ScanServer._dispatch

    def old_status(self):
        st = plain_status(self)
        st.pop("registry_rev", None)
        return st

    def old_dispatch(self, req):
        if req.get("cmd") == "get_registry":
            return {"ok": False, "error": "unknown command 'get_registry'"}
        return plain_dispatch(self, req)
    monkeypatch.setattr(ScanServer, "status_payload", old_status)
    monkeypatch.setattr(ScanServer, "_dispatch", old_dispatch)
    srv, win, c = remote_rig()
    b = win.builder
    pump(qapp, lambda: win.watch.last.get("phase") == 2)
    for _ in range(10):                               # a few polls
        pump(qapp, lambda: True)
        qapp.processEvents()
    assert win._proxy is None and win.watch.registry_reply is None
    assert b.registry is win.registry
    assert b.registry.get("slow") is None             # its own simulator, as before


def test_a_failing_get_registry_is_not_asked_again(qapp, remote_rig, monkeypatch):
    calls = []
    plain_dispatch = ScanServer._dispatch

    def refusing(self, req):
        if req.get("cmd") == "get_registry":
            calls.append(1)
            return {"ok": False, "error": "no"}
        return plain_dispatch(self, req)
    monkeypatch.setattr(ScanServer, "_dispatch", refusing)
    srv, win, c = remote_rig()
    pump(qapp, lambda: calls)
    n = len(calls)
    t_end = __import__("time").monotonic() + 1.5
    pump(qapp, lambda: __import__("time").monotonic() > t_end)
    assert len(calls) == n and win._proxy is None
    assert win.builder.registry is win.registry
