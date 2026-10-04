"""The service: wrap a Monochromator and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the brain. It runs two
threads of its own (the brain has a third, its hardware worker):
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies. A reply means ACCEPTED, not done: a
                  wavelength move takes seconds, watch `moving` in the status.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..control import ControlLease
from .. import secure
from ..monochromator import Monochromator
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Cs260Service:
    def __init__(self, mono: Monochromator,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.mono = mono
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
        #   SAFETY = verbs a VIEWER may always send:
        #   * `abort` -- stop the wavelength drive and drop queued moves;
        #   * `close_shutter` -- block the light at the exit slit (a viewer who
        #     sees light going onto a sample or a detector that must stay dark
        #     must be able to stop it). A verb of its own because `set_shutter`
        #     is NOT safety: the same verb also OPENS the shutter.
        #   Not safety: `step` / `calibrate` (they move or rewrite the drive).
        #   READ: none beyond get_/read_/list_ and the universal verbs.
        self.control = ControlLease(
            safety={"abort", "close_shutter"},
            read=set(),
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
        # keys"): when the lab's policy secures cs260, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "cs260",
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
        # route brain events into the publisher queue
        self.mono._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.mono.start()
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
        print(f"cs260 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.mono.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.mono.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.2) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.mono)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        pub = self._pub_sock                 # bound in start()
        last = 0.0
        while not self._stop.is_set():
            # forward any pending events immediately
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
        # linger, not 0: after `shutdown` the reply may still be queued, and
        # close(0) would drop it -- the launcher would then kill us anyway.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        try:
            if cmd == "set_wavelength":
                v = self.mono.set_wavelength(float(msg["wavelength_nm"]))
                return {"ok": True, "target_nm": v}
            elif cmd == "set_grating":
                return {"ok": True, "grating": self.mono.set_grating(int(msg["grating"]))}
            elif cmd == "set_shutter":
                self.mono.set_shutter(_bool(msg["open"]))
            elif cmd == "close_shutter":
                # a SAFETY verb: set_shutter(False), but a verb of its own so a
                # viewer may send it (it can only make things safer)
                self.mono.close_shutter()
            elif cmd == "set_filter":
                return {"ok": True, "filter": self.mono.set_filter(int(msg["filter"]))}
            elif cmd == "set_port":
                return {"ok": True, "port": self.mono.set_port(int(msg["port"]))}
            elif cmd == "step":
                self.mono.step(int(msg.get("steps", 0)))
            elif cmd == "abort":
                self.mono.abort()
            elif cmd == "calibrate":
                self.mono.calibrate(float(msg["wavelength_nm"]))
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.mono)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.mono.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.mono.cfg, msg["config"])
                self.mono.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to close its hardware (it wedged
                # the PM16 until replugged, docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop
                # ends serve_forever, whose finally: stop() shuts the brain down.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}
        except RuntimeError as exc:
            return {"ok": False, "error": f"{cmd}: {exc}"}

    def _info(self) -> dict:
        cfg = self.mono.cfg
        st = self.mono.status()
        grat = []
        for n in range(1, st.n_gratings + 1):
            lo, hi = self.mono.limits_for(n)
            lines, label = self.mono._grating_lines_label(n)
            grat.append({"n": n, "lines": lines, "label": label,
                         "min_nm": lo, "max_nm": hi})
        return {
            "idn": st.idn,
            "simulated": st.simulated,
            "gratings": grat,
            "filter_wheel": bool(cfg.accessories.filter_wheel),
            "dual_port": bool(cfg.accessories.dual_port),
            "wavelength_min_nm": st.wl_min_nm,
            "wavelength_max_nm": st.wl_max_nm,
        }


def _bool(v) -> bool:
    """A bool from the wire: accept true/false but also "off"/"0" as text
    (bool("false") would be True -- gotcha #3)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "open", "o")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
