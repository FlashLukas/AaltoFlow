"""Faults, dead services and the PAUSE (Lukas's decisions of 2026-09-28).

(A) A failed hardware read must be LOUD, and scan-core must not serve a dead
    service's last status frame forever.
(B) When the camera loses its pattern the measurement must stop or pause for
    the operator.

Plus the TARGET ECHO settle of the motion modules (gotcha #40) and the
superseding rule it needs: a newer command on a knob ends an older wait.

Layers, as elsewhere:
  * Instrument over the wire (a small fake service whose status we control,
    whose publisher we can silence, and which can send garbage);
  * Lab.faults / clear_fault;
  * the engine's pause, with a pure-Python registry (no sockets): stepped
    points, fly rows, headless stop, Abort while paused.

Ports 16700-16760 (the suite's are 5555+, the other scan-core tests 158xx-166xx).
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

from scan_core import Recipe, run                                   # noqa: E402
from scan_core import hooks as hooks_mod                            # noqa: E402
from scan_core.errors import Fault, ScanAborted, ScanFault          # noqa: E402
from scan_core.instrument import (Instrument, InstrumentError,      # noqa: E402
                                  InstrumentFault, adopt_then_flag, status_problem)
from scan_core.lab import Lab, build_lab_registry                   # noqa: E402
from scan_core.manifest import register_manifest, resolve_settle    # noqa: E402
from scan_core.registry import Gettable, Registry, Settable, build_sim_registry  # noqa: E402


# ───────────────────────────── a controllable fake ────────────────────────────

class Svc:
    """A service whose status is a plain dict the test edits.

    `publishing = False` silences the PUB stream while REP keeps answering;
    `answering = False` makes REP ignore requests (a hung service); `stop()`
    kills both (a crashed one). `junk()` sends malformed frames.

    `move_to{axis, um}` behaves like a kim-shaped stage with the TARGET ECHO
    of gotcha #40: the reply comes at once, the echo (`target_um[axis]`) and
    `moving` follow `adopt_delay` later, and the stage then travels at
    `speed` um/s. `step_um` rounds targets to whole steps, as kim does.
    """

    def __init__(self, port, status=None, manifest=None, adopt_delay=0.2,
                 speed=50.0, step_um=0.0):
        self.cmd_port, self.pub_port = port, port + 1
        self.st = status if status is not None else {}
        self.manifest = manifest
        self.adopt_delay, self.speed, self.step_um = adopt_delay, speed, step_um
        self.publishing = True
        self.answering = True
        self.commands = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._junk = []
        self._ctx = zmq.Context.instance()
        self._threads = []
        self._moves = {}                 # axis -> (t0, x0, target)

    def start(self):
        for fn in (self._serve, self._publish, self._physics):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)

    def junk(self, *frames):
        with self.lock:
            self._junk.extend(frames)

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.st))

    def _handle(self, msg):
        cmd = msg.get("cmd")
        self.commands.append(msg)
        if cmd == "info":
            return {"ok": True, "info": {}}
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "describe":
            if self.manifest is None:
                return {"ok": False, "error": "unknown command: 'describe'"}
            return {"ok": True, "describe": self.manifest}
        if cmd == "clear_fault":
            with self.lock:
                self.st["fault"] = ""
            return {"ok": True}
        if cmd == "move_to":
            axis, um = int(msg["axis"]), float(msg["um"])
            if self.step_um:
                um = round(um / self.step_um) * self.step_um
            threading.Thread(target=self._adopt, args=(axis, um), daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _adopt(self, axis, um):
        # the order gotcha #40 asks of a module: the move is issued first,
        # the echo target stored after -- and `moving` goes up with it
        time.sleep(self.adopt_delay)
        with self.lock:
            x0 = self.st["position_um"][axis]
            self._moves[axis] = (time.monotonic(), x0, um)
            self.st["target_um"][axis] = um
            self.st["moving"][axis] = True

    def _physics(self):
        while not self._stop.is_set():
            with self.lock:
                for axis, (t0, x0, tgt) in list(self._moves.items()):
                    d = tgt - x0
                    travel = self.speed * (time.monotonic() - t0)
                    if travel >= abs(d):
                        self.st["position_um"][axis] = tgt
                        self.st["moving"][axis] = False
                        del self._moves[axis]
                    else:
                        self.st["position_um"][axis] = x0 + np.sign(d) * travel
            time.sleep(0.01)

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if not self.answering:
                    time.sleep(0.05)
                    continue
                if poller.poll(50):
                    try:
                        reply = self._handle(rep.recv_json())
                    except Exception as exc:
                        reply = {"ok": False, "error": str(exc)}
                    rep.send_json(reply)
        finally:
            rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        try:
            while not self._stop.is_set():
                with self.lock:
                    junk, self._junk = self._junk, []
                for frame in junk:
                    pub.send_multipart(frame)
                if self.publishing:
                    pub.send_multipart([b"status", json.dumps(self.status()).encode()])
                time.sleep(0.05)
        finally:
            pub.close(0)


@pytest.fixture
def svc():
    made = []

    def make(port, **kw):
        s = Svc(port, **kw).start()
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


def _inst(port, **kw):
    kw.setdefault("timeout_ms", 500)
    return Instrument("fake", host="127.0.0.1", cmd_port=port, **kw)


def _wait_frame(inst, pred=lambda st: True, timeout=3.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        with inst._lock:
            d = dict(inst._latest)
        if d and pred(d):
            return d
        time.sleep(0.02)
    raise AssertionError("no status frame arrived")


# ─────────────────────── (A) a silent service is loud ─────────────────────────

def test_a_stale_cache_falls_back_to_asking_the_service(svc):
    s = svc(16700, status={"value": 1})
    inst = _inst(16700, stale_after_s=0.3)
    try:
        _wait_frame(inst)
        s.publishing = False                 # PUB dies, REP still answers
        time.sleep(0.5)
        with s.lock:
            s.st["value"] = 2
        # before 2026-09-28 the cache said 1 forever
        assert inst.status()["value"] == 2
    finally:
        inst.close()


def test_a_dead_service_is_an_error_not_its_last_frame(svc):
    s = svc(16702, status={"value": 1, "field_stable": True})
    inst = _inst(16702, stale_after_s=0.3, timeout_ms=300)
    try:
        _wait_frame(inst)
        s.stop()                             # crashed: no PUB, no REP
        time.sleep(0.5)
        with pytest.raises(InstrumentError, match="has not published"):
            inst.status()
    finally:
        inst.close()


def test_the_gui_poll_never_blocks_on_a_dead_service(svc):
    """The control panel polls on the GUI thread: it reads `latest()`, which
    never goes to the network -- `status()` on a dead service would freeze
    the window for the REQ timeout at every poll."""
    s = svc(16720, status={"value": 1})
    inst = _inst(16720, stale_after_s=0.3, timeout_ms=1500)
    try:
        _wait_frame(inst)
        assert inst.latest() == {"value": 1}
        s.stop()
        time.sleep(0.5)
        t0 = time.monotonic()
        assert inst.latest() is None          # stale: nothing, not the old frame
        assert time.monotonic() - t0 < 0.1
    finally:
        inst.close()


def test_the_listener_survives_malformed_frames(svc):
    s = svc(16704, status={"value": 1})
    inst = _inst(16704)
    events = []
    inst.on_event = lambda level, msg: events.append((level, msg))
    try:
        _wait_frame(inst)
        s.junk([b"only-one-part"], [b"status", b"{not json"],
               [b"status", b"[1, 2, 3]"], [b"a", b"b", b"c"])
        time.sleep(0.3)
        with s.lock:
            s.st["value"] = 7
        # still listening: the new value arrives through the PUB cache
        _wait_frame(inst, lambda st: st.get("value") == 7)
        assert inst.bad_frames >= 4
        assert any("malformed" in m for _, m in events)
    finally:
        inst.close()


# ──────────────────── settle waits do not trust hw_error ─────────────────────

def test_status_problem_reads_the_convention():
    assert status_problem({}) == ""
    assert status_problem({"hw_error": "", "fault": ""}) == ""
    assert status_problem({"hw_error": None, "fault": False}) == ""
    assert "timeout" in status_problem({"hw_error": "VISA timeout"})
    assert status_problem({"fault": "pattern lost"}) == "pattern lost"


def test_a_settle_wait_does_not_accept_a_frame_carrying_hw_error(svc):
    s = svc(16706, status={"done": True, "hw_error": "read failed"})
    inst = _inst(16706, fault_grace_s=0.4)
    try:
        _wait_frame(inst)
        t0 = time.monotonic()
        # the predicate is satisfied -- but by a frame whose read failed
        with pytest.raises(InstrumentFault, match="read failed"):
            inst.wait_until(lambda st: st.get("done"), timeout_s=5.0)
        assert time.monotonic() - t0 < 2.0   # the grace, not the timeout
    finally:
        inst.close()


def test_a_brief_hw_error_only_delays_the_settle(svc):
    s = svc(16708, status={"done": True, "hw_error": "glitch"})
    inst = _inst(16708, fault_grace_s=2.0)
    try:
        _wait_frame(inst)

        def clear():
            time.sleep(0.4)
            with s.lock:
                s.st["hw_error"] = ""
        threading.Thread(target=clear, daemon=True).start()
        t0 = time.monotonic()
        st = inst.wait_until(lambda st: st.get("done"), timeout_s=5.0)
        assert st["hw_error"] == "" and time.monotonic() - t0 >= 0.3
    finally:
        inst.close()


# ─────────────────────────── Lab.faults / clear_fault ─────────────────────────

CAM_MANIFEST = {
    "schema": 1, "module": "cam", "revision": 1,
    "parameters": [
        {"id": "x", "label": "x", "kind": "indicator", "type": "float",
         "unit": "um", "read_path": ["x"]},
        {"id": "clear_fault", "label": "Clear fault", "kind": "action",
         "type": "action"},
    ]}
METER_MANIFEST = {
    "schema": 1, "module": "meter", "revision": 1,
    "parameters": [
        {"id": "power", "label": "P", "kind": "indicator", "type": "float",
         "unit": "mW", "read_path": ["power"]}]}


def test_lab_faults_names_hw_error_fault_and_dead_services(svc):
    cam = svc(16710, status={"x": 1.0, "fault": "", "hw_error": ""},
              manifest=CAM_MANIFEST)
    meter = svc(16712, status={"power": 1.0, "hw_error": ""}, manifest=METER_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("cam", "meter"),
                                  ports={"cam": 16710, "meter": 16712},
                                  prefix=True, timeout_ms=400)
    try:
        for inst in lab.instruments.values():
            inst.stale_after_s = 0.4
        time.sleep(0.3)
        assert lab.faults() == []
        with cam.lock:
            cam.st["fault"] = "pattern lost"
        with meter.lock:
            meter.st["hw_error"] = "USB read failed"
        time.sleep(0.3)
        got = dict(lab.faults())
        assert got["cam"] == "pattern lost"
        assert "USB read failed" in got["meter"]
        # only the instruments a scan USES are asked about
        assert [f.name for f in reg.fault_check({"meter.power"})] == ["meter"]
        assert reg.fault_check({"nobody.knows"}) == []
        # the camera LATCHES its fault and offers clear_fault; the meter does not
        assert lab.can_clear_fault("cam") and not lab.can_clear_fault("meter")
        lab.clear_fault("cam")
        time.sleep(0.3)
        assert [f.name for f in lab.faults()] == ["meter"]
        # a dead service is a fault too, never its last frame
        meter.stop()
        time.sleep(0.6)
        got = dict(lab.faults())
        assert "not answering" in got["meter"]
    finally:
        lab.close()


# ──────────────── target echo: `index` on BOTH keys, and `tol` ────────────────

def _kim_manifest(tol=None):
    settle = {"policy": "adopt_then_flag", "setpoint_key": "target_um",
              "flag_key": "moving", "invert": True, "index": 1}
    if tol is not None:
        settle["tol"] = tol
    return {"schema": 1, "module": "kimlike", "revision": 1, "parameters": [
        {"id": "position_y", "label": "Y", "kind": "control", "type": "float",
         "unit": "um", "min": -1000, "max": 1000, "read_path": ["position_um", 1],
         "set": {"verb": "move_to", "arg": "um", "extra": {"axis": 1}},
         "settle": settle, "timeout_s": 8.0}]}


def _kim_status():
    return {"position_um": [0.0, 0.0, 0.0], "target_um": [0.0, 0.0, 0.0],
            "moving": [False, False, False]}


def test_adopt_then_flag_resolves_index_on_both_keys():
    make = resolve_settle({"policy": "adopt_then_flag", "setpoint_key": "target_um",
                           "flag_key": "moving", "invert": True, "index": 2})
    ok = make(5.0)
    assert ok({"target_um": [0, 0, 5.0], "moving": [True, True, False]})
    # the frame from BEFORE the move: old target, not moving -> NOT settled
    assert not ok({"target_um": [0, 0, 0.0], "moving": [False, False, False]})
    # the new target echoed, still moving -> not yet
    assert not ok({"target_um": [0, 0, 5.0], "moving": [False, False, True]})
    # other axes do not matter
    assert ok({"target_um": [9, 9, 5.0], "moving": [True, True, False]})


def test_the_echo_waits_out_the_stale_frame_that_fools_flag_only(svc):
    s = svc(16714, status=_kim_status(), manifest=_kim_manifest(), adopt_delay=0.3,
            speed=20.0)
    inst = _inst(16714)
    try:
        reg = Registry()
        register_manifest(reg, inst, _kim_manifest())
        p = reg.get("position_y")
        p.set(4.0)
        st = s.status()
        # arrived for real: the stage is AT the target, not merely "not moving"
        # on the frame from before the command
        assert st["position_um"][1] == pytest.approx(4.0)
        assert st["target_um"][1] == pytest.approx(4.0)
    finally:
        inst.close()


def test_a_stage_that_rounds_to_steps_settles_within_the_declared_tol(svc):
    s = svc(16716, status=_kim_status(), manifest=_kim_manifest(tol=0.02),
            step_um=0.021, speed=50.0)
    inst = _inst(16716)
    try:
        reg = Registry()
        register_manifest(reg, inst, _kim_manifest(tol=0.02))
        t0 = time.monotonic()
        reg.get("position_y").set(1.0)        # echoed as 48 steps = 1.008 um
        assert time.monotonic() - t0 < 3.0
        assert s.status()["target_um"][1] == pytest.approx(1.008)
    finally:
        inst.close()


def test_a_newer_command_ends_the_older_wait_promptly(svc):
    """The fly row's case: its move waits (in a helper thread) for the echo
    of the far target; the row is ended by a "stop here" setpoint, which
    replaces that target -- the old wait must end, not sit out its timeout."""
    s = svc(16718, status=_kim_status(), manifest=_kim_manifest(), speed=5.0)
    inst = _inst(16718)
    try:
        reg = Registry()
        register_manifest(reg, inst, _kim_manifest())
        p = reg.get("position_y")
        ended = {}

        def far():
            p.set(500.0, timeout_s=8.0)       # 100 s at 5 um/s
            ended["t"] = time.monotonic()
        th = threading.Thread(target=far, daemon=True)
        th.start()
        time.sleep(0.8)
        here = s.status()["position_um"][1]
        t_stop = time.monotonic()
        p.set(here + 0.1)                     # "stop here" (a setpoint just ahead)
        th.join(3.0)
        assert "t" in ended, "the superseded wait did not end"
        assert ended["t"] - t_stop < 1.5
    finally:
        inst.close()


# ─────────────────────── the engine: pause and redo ───────────────────────────

class Rig:
    """A pure-Python instrument set with a switchable fault.

    `fault_at` = the x value at which reading the detector RAISES the fault
    (it is latched then, like the camera's), and the reading taken that time
    is poison (999). Nothing is on the network.
    """

    def __init__(self, fault_at=None, clear_after=None, raise_in_set=False):
        self.x = 0.0
        self.sets = []
        self.fault = ""
        self.fault_at, self.clear_after = fault_at, clear_after
        self.raise_in_set = raise_in_set
        self.fault_seen = []
        self.reg = Registry()
        self.reg.add(Settable("x", "x", "um", (-100, 100), self._set, lambda: self.x))
        self.reg.add(Gettable("sig", "signal", "V", self._read))
        self.reg.fault_check = self.check
        self.checked_ids = []

    def _set(self, v):
        self.sets.append(v)
        if self.raise_in_set and v == self.fault_at and not self.fault_seen:
            # the fault appears WHILE this point settles, and the settle
            # times out because of it (the camera's point never settles)
            self._latch()
            raise TimeoutError("x never settled")
        self.x = v

    def _latch(self):
        self.fault = "pattern lost"
        self.fault_seen.append(time.monotonic())
        if self.clear_after is not None:
            threading.Timer(self.clear_after, self._clear).start()

    def _read(self):
        if (self.fault_at is not None and not self.raise_in_set
                and self.x == self.fault_at and not self.fault_seen):
            self._latch()
            return 999.0
        return 10.0 * self.x

    def _clear(self):
        self.fault = ""

    def check(self, ids=None):
        self.checked_ids.append(set(ids or ()))
        return [Fault("cam", self.fault)] if self.fault else []


def _line(num=5):
    return Recipe(name="line", axes=[{"type": "linear", "param": "x", "start": 0,
                                      "stop": num - 1, "num": num}],
                  detectors=["sig"])


def test_a_fault_pauses_and_the_point_is_measured_again():
    rig = Rig(fault_at=2.0, clear_after=0.3)
    seen = []
    ds = run(_line(), rig.reg, on_fault=lambda f: seen.append(list(f)),
             pause_poll_s=0.05)
    # the poisoned reading was thrown away; the redo measured the real value
    assert ds["sig"].values.tolist() == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert rig.sets.count(2.0) == 2          # x = 2 set again for the redo
    assert seen[0] == [("cam", "pattern lost")] and seen[-1] == []
    # only the ids the scan uses were asked about
    assert rig.checked_ids[0] == {"x", "sig"}


def test_without_a_pause_handler_a_fault_stops_the_scan_and_keeps_the_rest():
    rig = Rig(fault_at=2.0)
    ran_after = []
    hooks_mod.ACTIONS["_t_mark"] = lambda ctx, **kw: ran_after.append(1)
    try:
        r = _line()
        r.hooks = [{"when": "after_scan", "action": "_t_mark"}]
        with pytest.raises(ScanFault, match="pattern lost") as ei:
            run(r, rig.reg)
    finally:
        del hooks_mod.ACTIONS["_t_mark"]
    vals = ei.value.dataset["sig"].values
    assert vals[:2].tolist() == [0.0, 10.0]
    assert np.all(np.isnan(vals[2:]))        # NOT the poisoned 999
    assert ei.value.faults == [("cam", "pattern lost")]
    assert ran_after == [1]                  # an error: the after-scan routine runs (2026-10-06)


def test_abort_while_paused_is_an_ordinary_abort():
    rig = Rig(fault_at=1.0)                  # never clears
    ran_after = []
    hooks_mod.ACTIONS["_t_mark"] = lambda ctx, **kw: ran_after.append(1)
    abort = threading.Event()
    try:
        r = _line()
        r.hooks = [{"when": "after_scan", "action": "_t_mark"}]
        threading.Timer(0.4, abort.set).start()
        with pytest.raises(ScanAborted, match="paused") as ei:
            run(r, rig.reg, on_fault=lambda f: None, should_abort=abort.is_set,
                pause_poll_s=0.05)
    finally:
        del hooks_mod.ACTIONS["_t_mark"]
    assert ran_after == [1]                  # after_scan still runs on Abort
    assert ei.value.dataset["sig"].values[0] == 0.0


def test_an_error_while_a_fault_is_reported_pauses_instead_of_ending_the_scan():
    """A camera that lost its pattern never settles its point: the settle
    TIMEOUT is the symptom, the fault the cause -- pause for it."""
    rig = Rig(fault_at=2.0, clear_after=0.3, raise_in_set=True)
    ds = run(_line(), rig.reg, on_fault=lambda f: None, pause_poll_s=0.05)
    assert ds["sig"].values.tolist() == [0.0, 10.0, 20.0, 30.0, 40.0]


def test_an_error_without_a_fault_ends_the_scan_as_before():
    rig = Rig()
    rig.reg.get("x")._set = lambda v: (_ for _ in ()).throw(TimeoutError("boom"))
    with pytest.raises(TimeoutError, match="boom"):
        run(_line(), rig.reg, on_fault=lambda f: None, pause_poll_s=0.05)


def test_a_registry_without_a_fault_check_runs_unchanged():
    reg = build_sim_registry()
    assert getattr(reg, "fault_check", None) is None
    r = Recipe(name="t", axes=[{"type": "linear", "param": "field", "start": 0,
                                "stop": 10, "num": 3}], detectors=["lockin_r"])
    assert run(r, reg)["lockin_r"].shape == (3,)


def test_a_faulted_fly_row_is_flown_again():
    reg = build_sim_registry()
    state = {"calls": 0, "until": 0.0}
    seen = []

    def check(ids=None):
        # the first check (at the end of row 1) finds the camera without its
        # pattern, and it stays lost for 0.3 s
        state["calls"] += 1
        if state["calls"] == 1:
            state["until"] = time.monotonic() + 0.3
        if time.monotonic() < state["until"]:
            return [Fault("cam", "pattern lost")]
        return []
    reg.fault_check = check

    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
               axes=[{"type": "linear", "param": "pos_y", "start": -1, "stop": 1,
                      "num": 2},
                     {"type": "fly", "param": "pos_x", "start": -10, "stop": 10,
                      "num": 21, "speed": 40, "speed_param": "stage_speed"}],
               detectors=["lockin_r"])
    pauses = []
    ds = run(r, reg, on_log=seen.append,
             on_fault=lambda f: pauses.append(list(f)), pause_poll_s=0.05)
    assert any("PAUSED at row 1" in m for m in seen), seen
    assert any("measuring row 1 of 2 again" in m for m in seen), seen
    assert pauses[0] == [("cam", "pattern lost")] and pauses[-1] == []
    assert np.all(np.isfinite(ds["lockin_r"].values))    # both rows complete


def test_a_faulted_fly_row_stops_a_headless_scan():
    reg = build_sim_registry()
    reg.fault_check = lambda ids=None: [Fault("cam", "pattern lost")]
    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
               axes=[{"type": "fly", "param": "pos_x", "start": -10, "stop": 10,
                      "num": 21, "speed": 40, "speed_param": "stage_speed"}],
               detectors=["lockin_r"])
    with pytest.raises(ScanFault) as ei:
        run(r, reg)
    # the row flown during the fault is not in the data
    assert np.all(np.isnan(ei.value.dataset["lockin_r"].values))


# ─────────────────────────── the GUI: the PAUSED banner ───────────────────────

def _gui_builder(check):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    reg.fault_check = check
    win = ScanBuilder(reg)
    win.add_axis("field")
    win.rows[0].num.setValue(4)
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


class FakeLab:
    """What the banner needs from a Lab: which module latches, and the verb."""

    def __init__(self, state):
        self.state, self.cleared = state, []

    def can_clear_fault(self, name):
        return name == "camera"

    def clear_fault(self, name):
        self.cleared.append(name)
        self.state["fault"] = ""


def test_the_gui_shows_paused_offers_clear_fault_and_resumes():
    state = {"fault": "", "calls": 0}

    def check(ids=None):
        state["calls"] += 1
        if state["calls"] == 3:               # during the 2nd point: latched
            state["fault"] = "pattern lost"
        return [Fault("camera", state["fault"])] if state["fault"] else []
    win = _gui_builder(check)
    lab = FakeLab(state)
    win.fault_lab = lab
    try:
        win._start_worker(win.build_recipe())
        assert _pump_until(lambda: win.pause_box.isVisibleTo(win))
        assert "camera: pattern lost" in win.pause_lbl.text()
        assert "PAUSED" in win.progress.format()
        assert win.abort_btn.isEnabled()      # Abort works while paused
        btn = win.clear_fault_btns["camera"]
        assert btn.text() == "Clear fault on camera"
        btn.click()
        assert lab.cleared == ["camera"]
        assert _pump_until(lambda: win.worker is None, timeout=15.0)
        assert not win.pause_box.isVisibleTo(win)
        assert np.all(np.isfinite(win.dataset["lockin_r"].values))
        assert any("resuming" in m for m in win.run_log)
    finally:
        win.close()


def test_abort_from_the_paused_banner_ends_the_run():
    win = _gui_builder(lambda ids=None: [Fault("pm16", "hardware read failed: USB")])
    try:
        win._start_worker(win.build_recipe())
        assert _pump_until(lambda: win.pause_box.isVisibleTo(win))
        assert win.clear_fault_btns == {}     # no Lab: nothing to clear
        win.abort_btn.click()
        assert _pump_until(lambda: win.worker is None, timeout=15.0)
        assert not win.pause_box.isVisibleTo(win)
        assert "aborted" in win.detail.text()
    finally:
        win.close()
