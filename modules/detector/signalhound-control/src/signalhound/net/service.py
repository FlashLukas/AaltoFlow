"""The service: wrap a SpectrumAnalyzer and expose it over ZeroMQ.

One process owns the analyser (simulated or real). Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The brain's own sweep thread does the measuring.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..control import ControlLease
from .. import secure
from ..instruments import TG_RANGE_HZ
from ..spectrum import SpectrumAnalyzer
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe, trace_to_wire)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class SignalhoundService:
    def __init__(self, signalhound: SpectrumAnalyzer,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.signalhound = signalhound
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send: `abort` (cancel the running
        #   acquisition) and `tg_abort` (cancel a tracking-generator sweep; the
        #   analyser is then restored). Both only STOP something. `tg_cw` is
        #   not in the list even with on=false: the same verb also retunes the
        #   TG, and the TG has no real "off" (it parks). The client modules
        #   shsg / shsna drive the TG as kind "machine", so a person's GUI
        #   holding control never locks them out.
        #   READ = read-only verbs whose names do not start with get_/read_/
        #   list_: `tg_grid` only COMPUTES the grid a TG sweep would use
        #   (sweeps nothing, sends nothing to the analyser).
        self.control = ControlLease(
            safety={"abort", "tg_abort"},
            read={"tg_grid"},
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
        # keys"): when the lab's policy secures signalhound, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "signalhound",
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
        self.signalhound._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.signalhound.start()
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
        kind = "simulated" if self.signalhound.simulated else "REAL"
        print(f"signalhound service up ({kind})  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        secure.release_server(self._guard)
        self._guard = None
        self.signalhound.shutdown()
        print("signalhound service stopped")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.signalhound.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes with centre/span (each bounds the other), the grid's point
        count and the RBW (VBW's maximum)."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.signalhound)["revision"]
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
                try:
                    # the raw frame, not recv_json: the frame carries the
                    # CurveZMQ key that sent it, which the guard checks
                    frame = rep.recv(copy=False)
                    msg = json.loads(frame.bytes.decode("utf-8"))
                    # security first: does the identity in the request match
                    # the key that sent it? (None = yes, or security is off)
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
        v = self.signalhound
        try:
            if cmd == "set_center":
                v.set_center(float(msg["center_Hz"]))
            elif cmd == "set_span":
                v.set_span(float(msg["span_Hz"]))
            elif cmd == "set_start_stop":
                v.set_start_stop(float(msg["start_Hz"]), float(msg["stop_Hz"]))
            elif cmd == "set_ref_level":
                v.set_ref_level(float(msg["ref_level_dBm"]))
            elif cmd == "set_rbw":
                v.set_rbw(float(msg["rbw_Hz"]))
            elif cmd == "set_vbw":
                v.set_vbw(float(msg["vbw_Hz"]))
            elif cmd == "set_reject":
                v.set_reject(_bool(msg["on"]))
            elif cmd == "set_detector":
                v.set_detector(str(msg["detector"]))
            elif cmd == "set_averages":
                v.set_averages(int(round(float(msg["averages"]))))
            elif cmd == "set_continuous":
                v.set_continuous(_bool(msg["on"]))
            elif cmd == "set_scene":
                v.set_scene(str(msg["name"]), msg["value"])
            elif cmd == "acquire":
                return {"ok": True, "acq_id": v.acquire()}
            elif cmd == "abort":
                v.abort()
            # ---- the TG contract, for the client modules shsg / shsna --------
            # (README "For client modules: the TG contract"). A refusal (no
            # TG, out of range, busy) is not a malformed request: its reason
            # goes back as the error, without a "bad request" prefix.
            elif cmd in ("tg_cw", "tg_sweep_acquire", "get_tg_trace"):
                # `on` is optional: a frequency / level change alone keeps the
                # TG on or parked as it is (shsg retunes while its RF is off)
                on = (_bool(msg["on"]) if cmd == "tg_cw" and "on" in msg else None)
                try:
                    if cmd == "tg_cw":
                        out = v.tg_cw(on, _opt(msg, "freq_hz"), _opt(msg, "level_dbm"))
                        # deferred True: accepted while a TG sweep holds the
                        # TG; applied (and echoed in status) when it ends
                        deferred = bool(out.pop("deferred", False))
                        return {"ok": True, "tg_cw": json_safe(out), "deferred": deferred}
                    if cmd == "tg_sweep_acquire":
                        n = v.tg_sweep_acquire(
                            float(msg["start_hz"]), float(msg["stop_hz"]),
                            _opt(msg, "level_dbm"), _opt(msg, "rbw_hz"),
                            _opt(msg, "averages"), _opt(msg, "points"))
                        # + what was accepted: points (clamped to 1001) and
                        # level_applied False (the TG sweep ignores the level)
                        info = v.tg_request(n)
                        return {"ok": True, "tg_acq_id": n,
                                **{k: x for k, x in info.items() if k != "id"}}
                    t = v.get_tg_trace(None if msg.get("id") is None else int(msg["id"]))
                    return {"ok": True, **json_safe(t)}
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
            elif cmd == "tg_abort":
                # optional id: abort only that one (ok either way; "aborted" says)
                n = msg.get("id")
                return {"ok": True, "aborted": v.tg_abort(None if n is None else int(n))}
            elif cmd == "tg_grid":
                try:
                    return {"ok": True, **json_safe(v.tg_grid(
                        float(msg["start_hz"]), float(msg["stop_hz"]),
                        _opt(msg, "points"), _opt(msg, "rbw_hz")))}
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
            elif cmd == "get_trace":
                try:
                    t = v.get_trace(str(msg.get("which", "sample")),
                                    str(msg.get("quantity", "trace")))
                except ValueError as exc:
                    # "nothing yet", "aborted": not a malformed request, so no
                    # "bad request" prefix -- the reason is the message
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, **trace_to_wire(t)}
            elif cmd == "get_frequencies":
                f = v.frequencies()
                return {"ok": True, "values": f.tolist(), "values_GHz": (f / 1e9).tolist()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(v.get_sample())}
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
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (suite gotcha #25). The reply still goes out: the commander
                # sends it before it looks at _stop again.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        v = self.signalhound
        lim = v.cfg.limits
        st = v.status()
        lo, hi = v.freq_range()
        return {
            "idn": st.idn, "simulated": v.simulated, "model": st.device_model,
            "tg_attached": st.tg_attached, "detectors": ["average", "peak"],
            "freq_min_Hz": lo, "freq_max_Hz": hi,
            "rbw_min_Hz": lim.rbw_min_Hz, "rbw_max_Hz": v.rbw_max(),
            "ref_min_dBm": lim.ref_min_dBm, "ref_max_dBm": lim.ref_max_dBm,
            # the TG envelope, for the client modules (shsg / shsna)
            "tg_freq_min_Hz": TG_RANGE_HZ[0], "tg_freq_max_Hz": TG_RANGE_HZ[1],
            "tg_sweep_min_Hz": v.tg_sweep_range()[0], "tg_sweep_max_Hz": v.tg_sweep_range()[1],
            "tg_level_min_dBm": v.tg_level_range()[0], "tg_level_max_dBm": v.tg_level_range()[1],
            "tg_points_min": lim.tg_points_min, "tg_points_max": lim.tg_points_max,
            "tg_cw_during_sweep": bool(v.cfg.hardware.tg_cw_during_sweep),
        }


def _opt(msg: dict, key: str):
    """An optional numeric argument: missing or null -> None, else float."""
    x = msg.get(key)
    return None if x is None else float(x)


def _bool(x) -> bool:
    """A JSON bool, or a number / string from a hand-typed command. bool("false")
    would be True (gotcha #3), so strings are parsed."""
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "on")
    return bool(x)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
