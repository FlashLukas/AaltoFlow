"""The service: wrap a Scope and expose it over ZeroMQ.

One process owns the scope (simulated or real). Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The Scope's own trace thread does the measuring.

Verbs (a reply means ACCEPTED; poll status for the effect):
  scope settings   set_channel_enabled {channel, on}  set_vdiv {channel, vdiv_V}
                   set_offset {channel, offset_V}  set_coupling {channel, coupling}
                   set_probe {channel, probe}  set_tdiv {tdiv_s}  set_delay {delay_s}
                   set_trigger_source {source}  set_trigger_level {level_V}
                   set_trigger_slope {slope}  set_trigger_mode {mode}
  module settings  set_points {points}  set_averages {averages}  set_keep_raw {on}
                   set_filter {lowpass_Hz?, highpass_Hz?, order?}
                   set_physical {channel, scale?, offset?, unit?, label?}
                   restart_average {}  set_sim {name, value} (simulator)
  measuring        acquire {} -> {acq_id}   abort {} (safety)   stop {} (safety, = abort)
                   get_trace {which: "live"|"sample"}   get_time {}   get_sample {}
  supplies         set_supply {supply: "vplus"|"vminus", on?, volts?}
                   supplies_off {} (safety)                (an instrument with supplies)
  generator        gen_<verb> = afg-control's verbs for the outputs W1/W2
                   (an instrument with a generator, e.g. the Analog Discovery):
                   gen_set_output {channel: "w1"|"w2", on}  gen_set_waveform
                   gen_set_frequency  gen_set_amplitude  gen_set_offset  gen_set_phase
                   gen_set_duty  gen_set_symmetry  gen_set_follow {on, phase_offset_deg?,
                   phase?}  gen_set_phase_follow {on}  gen_set_phase_offset {deg}
                   gen_align_phase {} -> {op_id}  gen_outputs_off {} -> {op_id} (safety)
                   gen_get_config {}  gen_set_config {config}  gen_info {}
  + status, info, get_config, set_config, describe, shutdown.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..control import ControlLease
from .. import secure
from ..scope import Scope
from .describe import build_manifest
from ..generator import wire
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe, trace_to_wire)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class ScopeService:
    def __init__(self, scope: Scope,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.scope = scope
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        # set by shutdown{keep_outputs: true}: a RESTART (code update) that must
        # not change what the instrument outputs; the next start adopts it
        self._keep_outputs = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send: `abort` / `stop` (cancel the
        #   running acquisition). A scope drives nothing, so stopping a
        #   measurement is the only "make safe" there is. (A backend with a
        #   generator -- the Analog Discovery -- will add generator_off.)
        #   `acquire` is a trigger (it replaces the sample a scan waits on),
        #   `restart_average` empties what someone else is watching: not safety.
        #   READ: none beyond get_/read_/list_ and the universal verbs
        #   (get_trace, get_time, get_sample are reads by their names).
        # With a generator / supplies (the Analog Discovery) the "make it
        # safe" actions are theirs too: every output off, every supply off --
        # verbs that can ONLY switch off, so a viewer may always send them.
        self.control = ControlLease(
            safety={"abort", "stop", "gen_outputs_off", "supplies_off"},
            # the generator's reads carry the gen_ prefix, so the get_/read_
            # rule does not see them: named here
            read={"gen_info", "gen_envelope", "gen_get_config"},
            on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # Bind BOTH sockets here, in the caller's thread, and BEFORE the
        # instrument is opened (gotcha #39). They used to be bound inside the
        # two daemon threads: a port already in use then killed only that
        # thread, with a traceback nobody reads, while the process lived on --
        # deaf, but holding the instrument and its hwlock claim. Now a taken
        # port raises PortInUse out of start(), before anything was opened or
        # claimed. (Handing a socket to the thread that will use it is allowed
        # in ZeroMQ; Thread.start() is the memory barrier it asks for.)
        self._pub_sock = self._ctx.socket(zmq.PUB)
        self._rep_sock = self._ctx.socket(zmq.REP)
        # Encryption and who-is-who (secure.py, README "Encryption and
        # keys"): when the lab's policy secures scope, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "scope",
                on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))
        except secure.SecurityError:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise
        try:
            self._pub_sock.bind(self.pub_addr)
            self._rep_sock.bind(self.cmd_addr)
        except zmq.ZMQError as exc:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise PortInUse(
                f"cannot listen on {self.cmd_addr} / {self.pub_addr} ({exc}); "
                f"is another service already using these ports?") from exc
        self.scope._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.scope.start()
        except BaseException:
            # the instrument did not start (busy, unplugged, ...):
            # give the ports back before the exception leaves
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        kind = "simulated" if self.scope.simulated else "REAL"
        print(f"scope service up ({kind})  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        time.sleep(self.status_dt + 0.1)
        # the service is gone: drop its guard (and its "running encrypted" marker)
        secure.release_server(self._guard)
        self._guard = None
        self.scope.shutdown(keep_outputs=self._keep_outputs)
        print("scope service stopped")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.scope.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes with the averages (the acquisition timeout), the trace
        length and keep_raw (the detectors' shape), and the channels' units."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.scope)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        pub = self._pub_sock                 # bound in start()
        last = 0.0
        while not self._stop.is_set():
            try:
                while True:
                    ev = self._events.get_nowait()
                    pub.send_multipart([TOPIC_EVENT, _json(ev)])
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last >= self.status_dt:
                pub.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                last = now
            time.sleep(0.01)
        pub.close(0)

    def _commander(self) -> None:
        rep = self._rep_sock                 # bound in start()
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                # recv(copy=False) keeps the frame, which carries the key of
                # the PC that sent it (the guard checks it below)
                try:
                    frame = rep.recv(copy=False)
                except zmq.ZMQError:
                    continue                  # nothing received: nothing to answer
                try:
                    msg = json.loads(frame.bytes.decode("utf-8"))
                    # security first: does the identity match the key that
                    # sent it? (None = yes, or security is off)
                    refused = None
                    if self._guard is not None and isinstance(msg, dict):
                        refused = self._guard.check(msg, secure.user_id(frame))
                    rep.send_json(refused or self._dispatch(msg))
                except Exception as exc:                       # never let the loop die
                    try:
                        rep.send_json({"ok": False, "error": str(exc)})
                    except zmq.ZMQError:
                        pass
        # linger, not 0: after `shutdown` the reply may still be in the queue,
        # and close(0) would throw it away (suite gotcha #25).
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        v = self.scope
        gv = wire.verb(str(cmd or ""))
        if gv is not None:
            return self._gen_dispatch(gv, msg)
        try:
            ch = msg.get("channel")
            if cmd == "set_supply":
                on = msg.get("on")
                v.set_supply(str(msg["supply"]), on=None if on is None else _bool(on),
                             volts=_opt_float(msg, "volts"))
            elif cmd == "supplies_off":
                v.supplies_off()
            elif cmd == "set_channel_enabled":
                v.set_channel_enabled(ch, _bool(msg["on"]))
            elif cmd == "set_vdiv":
                v.set_vdiv(ch, float(msg["vdiv_V"]))
            elif cmd == "set_offset":
                v.set_offset(ch, float(msg["offset_V"]))
            elif cmd == "set_coupling":
                v.set_coupling(ch, str(msg["coupling"]))
            elif cmd == "set_probe":
                v.set_probe(ch, float(msg["probe"]))
            elif cmd == "set_tdiv":
                v.set_tdiv(float(msg["tdiv_s"]))
            elif cmd == "set_delay":
                v.set_delay(float(msg["delay_s"]))
            elif cmd == "set_trigger_source":
                v.set_trigger_source(str(msg["source"]))
            elif cmd == "set_trigger_level":
                v.set_trigger_level(float(msg["level_V"]))
            elif cmd == "set_trigger_slope":
                v.set_trigger_slope(str(msg["slope"]))
            elif cmd == "set_trigger_mode":
                v.set_trigger_mode(str(msg["mode"]))
            elif cmd == "set_points":
                v.set_points(int(round(float(msg["points"]))))
            elif cmd == "set_averages":
                v.set_averages(int(round(float(msg["averages"]))))
            elif cmd == "set_keep_raw":
                v.set_keep_raw(_bool(msg["on"]))
            elif cmd == "set_filter":
                v.set_filter(_opt_float(msg, "lowpass_Hz"), _opt_float(msg, "highpass_Hz"),
                             None if msg.get("order") is None else int(round(float(msg["order"]))))
            elif cmd == "set_physical":
                v.set_physical(ch, _opt_float(msg, "scale"), _opt_float(msg, "offset"),
                               msg.get("unit"), msg.get("label"))
            elif cmd == "restart_average":
                v.restart_average()
            elif cmd == "set_sim":
                v.set_sim(str(msg["name"]), msg["value"])
            elif cmd == "acquire":
                return {"ok": True, "acq_id": v.acquire()}
            elif cmd in ("abort", "stop"):
                v.abort()
            elif cmd == "get_trace":
                try:
                    t = v.get_trace(str(msg.get("which", "sample")))
                except ValueError as exc:
                    # "aborted", "not latched", "no trace yet": not a malformed
                    # request, so no prefix -- the reason is the message
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, **trace_to_wire(t)}
            elif cmd == "get_time":
                return {"ok": True, "values": json_safe(v.get_time())}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(v.status()["sample"])}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(v)}
            elif cmd == "info":
                return {"ok": True, "info": json_safe(self._info())}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(v.cfg)}
            elif cmd == "set_config":
                apply_config_dict(v.cfg, msg["config"])
                v.apply_config()
                self._rev_at = 0.0          # shapes may have moved: recompute now
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (suite gotcha #25). The reply still goes out: the commander
                # sends it before it looks at _stop again.
                # keep_outputs=true: a restart for a code update. A scope drives
                # nothing, so it closes exactly as a plain stop does -- the
                # reply says the outputs were kept (nothing was changed).
                self._keep_outputs = _bool(msg.get("keep_outputs", False))
                self._stop.set()
                return {"ok": True, "stopping": True,
                        "kept_outputs": self._keep_outputs}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            if cmd in ("set_points", "set_averages", "set_keep_raw", "set_physical",
                       "set_channel_enabled", "set_trigger_source"):
                self._rev_at = 0.0          # the manifest moved: recompute at once
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"{cmd}: {exc}"}

    def _gen_dispatch(self, cmd: str, msg: dict) -> dict:
        """A generator verb (gen_ prefix removed): afg-control's dispatch,
        copied, on the scope's generator brain."""
        g = self.scope.gen
        if g is None:
            return {"ok": False, "error": "this instrument has no generator"}
        try:
            ch = msg.get("channel")
            if cmd == "set_output":
                g.set_output(ch, _bool(msg["on"]))
            elif cmd == "set_waveform":
                g.set_waveform(ch, str(msg["waveform"]))
            elif cmd == "set_frequency":
                g.set_frequency(ch, float(msg["frequency_Hz"]))
            elif cmd == "set_amplitude":
                g.set_amplitude(ch, float(msg["amplitude_Vpp"]))
            elif cmd == "set_offset":
                g.set_offset(ch, float(msg["offset_V"]))
            elif cmd == "set_phase":
                g.set_phase(ch, float(msg["phase_deg"]))
            elif cmd == "set_duty":
                g.set_duty(ch, float(msg["duty_pct"]))
            elif cmd == "set_symmetry":
                g.set_symmetry(ch, float(msg["symmetry_pct"]))
            elif cmd == "set_load":
                g.set_load(ch, msg["load"])
            elif cmd == "set_follow":
                off, ph = msg.get("phase_offset_deg"), msg.get("phase")
                g.set_follow(_bool(msg["on"]), None if off is None else float(off),
                             None if ph is None else _bool(ph))
            elif cmd == "set_phase_follow":
                g.set_phase_follow(_bool(msg["on"]))
            elif cmd == "set_phase_offset":
                g.set_phase_offset(float(msg["deg"]))
            elif cmd == "align_phase":
                return {"ok": True, "op_id": g.align_phase()}
            elif cmd == "outputs_off":
                return {"ok": True, "op_id": g.outputs_off()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(g.cfg)}
            elif cmd == "set_config":
                apply_config_dict(g.cfg, msg["config"])
                g.apply_config()
                self._rev_at = 0.0
            elif cmd == "info":
                return {"ok": True, "info": json_safe({
                    "model": g.caps.get("model", ""), "channels": list(g.channels),
                    "waveforms": list(g.caps.get("waveforms", ())),
                    "ramp_symmetry": bool(g.caps.get("ramp_symmetry", True)),
                    "load_settable": bool(g.caps.get("load_settable", False)),
                    "phase_align": bool(g.caps.get("phase_align", False)),
                    "limits": {c: dict(vars(g.cfg.limits(c))) for c in g.channels},
                    "envelope": {c: g.envelope(c) for c in g.channels}})}
            elif cmd == "envelope":
                return {"ok": True, "envelope": json_safe(g.envelope(ch))}
            else:
                return {"ok": False, "error": f"unknown command: gen_{cmd!s}"}
            if cmd in ("set_waveform", "set_follow", "set_phase_follow"):
                self._rev_at = 0.0          # the manifest's shape follows these
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"gen_{cmd}: {exc}"}

    def _info(self) -> dict:
        v = self.scope
        st = v.status()
        return {"idn": st["idn"], "simulated": v.simulated, "model": st["model"],
                "channels": st["channels"], "generator_channels": st["generator_channels"],
                "ext_trigger": bool(v.caps.get("ext_trigger", False)),
                "trigger_sources": v.trigger_sources(),
                "trigger_options": {s: v.trigger_options(s) for s in v.trigger_sources()},
                "couplings": v.couplings(),
                "supplies": ({k: list(v.supply_limits(k)) for k in ("vplus", "vminus")}
                             if v.has_supplies() else {}),
                "units": {ch: st[f"{ch}_unit"] for ch in st["channels"]}}


def _opt_float(msg: dict, key: str):
    return None if msg.get(key) is None else float(msg[key])


def _bool(x) -> bool:
    """JSON true/false, but also "off"/"false"/0 from a hand-typed client:
    bool("false") is True, the same trap as the .ini (gotcha #3)."""
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "on")
    return bool(x)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
