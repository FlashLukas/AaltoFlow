"""control.py: one controller, many viewers -- the service-side rules."""

from suite_common import control as C


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ident(name, kind="gui"):
    i = C.make_identity(kind, name)
    return i


def req(cmd, who=None, **kw):
    r = {"cmd": cmd, **kw}
    if who is not None:
        r["client"] = who
    return r


def lease(**kw):
    clock = Clock()
    events = []
    l = C.ControlLease(safety={"stop", "kill_af"}, read={"stream_read"},
                       clock=clock, on_event=lambda lv, m: events.append((lv, m)), **kw)
    return l, clock, events


def test_nobody_holds_control_so_everything_passes_as_before():
    l, _, _ = lease()
    assert l.handle(req("move_to_um", axis="X", position=1)) is None
    assert l.handle(req("move_to_um", ident("a"))) is None
    assert l.status()["holder"] is None


def test_first_take_wins_and_a_viewer_is_refused_with_who_and_how():
    l, _, events = lease()
    a, b = ident("kim GUI"), ident("kim GUI 2")
    r = l.handle(req("take_control", a))
    assert r["ok"] and r["granted"]
    r = l.handle(req("take_control", b))            # not forced: stays a viewer
    assert r["ok"] and not r["granted"]
    assert l.handle(req("move_steps", a)) is None     # the holder
    r = l.handle(req("move_steps", b))
    assert r["ok"] is False and r["refused"] == "control"
    assert "kim GUI" in r["error"] and "take control" in r["error"]
    r = l.handle(req("move_steps"))                   # anonymous script
    assert r["ok"] is False and "did not say who sent it" in r["error"]
    assert any("has control" in m for _, m in events)


def test_reads_safety_shutdown_and_machines_always_pass():
    l, _, _ = lease()
    a, b = ident("a"), ident("b")
    l.handle(req("take_control", a))
    for cmd in ("status", "info", "describe", "get_config", "get_frame",
                "read_xy", "list_objectives", "stream_read", "stop", "kill_af",
                "shutdown", "heartbeat", "clients"):
        assert l.handle(req(cmd, b)) is None or l.handle(req(cmd, b))["ok"], cmd
    cam = ident("camera", kind="machine")
    assert l.handle(req("move_to_step", cam)) is None


def test_forced_take_over_is_reported_and_the_old_holder_becomes_a_viewer():
    l, _, events = lease()
    a, b = ident("a"), ident("b")
    l.handle(req("take_control", a))
    r = l.handle(req("take_control", b, force=True))
    assert r["granted"] and r["taken_from"]["id"] == a["id"]
    assert l.status()["holder"]["id"] == b["id"]
    assert l.handle(req("set_voltage", a))["refused"] == "control"
    assert any(lv == "warn" and "took over" in m for lv, m in events)


def test_a_silent_holder_loses_control_after_the_lease():
    l, clock, events = lease(lease_s=10)
    a, b = ident("a"), ident("b")
    l.handle(req("take_control", a))
    clock.t += 9
    l.handle(req("heartbeat", a))                    # keeps it
    clock.t += 9
    assert l.status()["holder"]["id"] == a["id"]
    clock.t += 11                                    # silent too long
    assert l.status()["holder"] is None
    assert any("went silent" in m for _, m in events)
    assert l.handle(req("move_steps", b)) is None     # free again


def test_release_and_status_lists_the_viewers():
    l, _, _ = lease()
    a, b = ident("a"), ident("b")
    l.handle(req("take_control", a))
    l.handle(req("heartbeat", b))
    st = l.status()
    assert {c["id"] for c in st["clients"]} == {a["id"], b["id"]}
    r = l.handle(req("release_control", b))          # not the holder
    assert r["released"] is False
    r = l.handle(req("release_control", a))
    assert r["released"] is True and r["control"]["holder"] is None


def test_take_needs_an_identity_and_unknown_commands_are_not_writes():
    l, _, _ = lease()
    assert l.handle(req("take_control"))["ok"] is False
    assert not l.is_write(None)
    assert l.is_write("move_to_um") and l.is_write("set_config")
    assert not l.is_write("get_px_calibration")


def test_the_client_mixin_speaks_the_protocol():
    """ControlClient against the lease, with an _rpc that calls it directly."""
    l, _, _ = lease()

    class Client(C.ControlClient):
        def __init__(self, name):
            self._control_setup("gui", name)

        def _rpc(self, **r):
            self._with_identity(r)
            reply = l.handle(r) or {"ok": True}
            if not reply.get("ok"):
                self._raise_refusal(reply)
                raise RuntimeError(reply.get("error"))
            return reply

    a, b = Client("a"), Client("b")
    assert a.take_control() and a.has_control()
    assert not b.take_control() and not b.has_control()
    try:
        b._rpc(cmd="move_steps")
        raise AssertionError("not refused")
    except C.ControlRefused as exc:
        assert "has control" in str(exc)
    assert b.take_control(force=True) and b.has_control()
    a._control_from_status({"control": l.status()})
    assert not a.has_control()
    assert b.release_control()
    assert b.control()["holder"] is None
