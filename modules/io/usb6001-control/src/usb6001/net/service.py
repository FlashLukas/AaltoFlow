"""The service: wrap a Daq and expose it over ZeroMQ.

One process owns the card (or the simulator) and the Daq brain. It runs
two extra threads, exactly like clMag's service:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies.

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
from ..daq import Daq
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Usb6001Service:
    def __init__(self, daq: Daq,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.gen = daq            # `gen` kept as the attribute name the template used
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = none. This is a GENERAL-purpose DAQ: what hangs on its
        #   outputs is not known here, so no value is "safe" in general (0 V on
        #   an AO, or a low DO line, can just as well switch something ON).
        #   Its only output verbs, `set_ao` and `set_do`, can drive anywhere,
        #   and it runs no task a viewer could stop. The per-line `safe_state`
        #   in the config is written on a clean shutdown only (daq.py).
        #   `acquire` is not safety either (guide: acquisition triggers replace
        #   the sample other clients wait on), nor is `save_config` (it writes
        #   a file on the service PC).
        #   READ = none extra: read_ai / read_di / get_sample are already read
        #   verbs by their prefix.
        self.control = ControlLease(
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
        try:
            self._pub_sock.bind(self.pub_addr)
            self._rep_sock.bind(self.cmd_addr)
        except zmq.ZMQError as exc:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise PortInUse(
                f"cannot listen on {self.cmd_addr} / {self.pub_addr} ({exc}); "
                f"is another service already using these ports?") from exc
        # route brain events into the publisher queue
        self.gen._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.gen.start()
        except BaseException:
            # the instrument did not start (busy, unplugged, ...):
            # give the ports back before the exception leaves
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"usb6001 service up | commands {self.cmd_addr} | status {self.pub_addr}")
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
        self.gen.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.gen.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.gen)["revision"]
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
                try:
                    msg = rep.recv_json()
                    rep.send_json(self._dispatch(msg))
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
        d = self.gen
        try:
            if cmd == "set_ao":
                v = d.set_ao(msg["channel"], float(msg["volts"]))
                return {"ok": True, "volts": v}
            elif cmd == "set_do":
                return {"ok": True, "state": d.set_do(msg["line"], msg["state"])}
            elif cmd == "read_ai":
                # A FRESH reading: waits for the poll thread's next read (at most
                # one poll period + one reading, well inside a client's 3 s).
                return {"ok": True, **d.read_ai(msg.get("channel"))}
            elif cmd == "read_di":
                return {"ok": True, **d.read_di(msg.get("line"))}
            elif cmd == "acquire":
                # fire-and-forget: the number to wait for, at once (gotcha #17)
                return {"ok": True, "acq_id": d.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": d.get_sample()}
            elif cmd == "save_config":
                return {"ok": True, "path": d.save_config(msg.get("path"))}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(d)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(d.cfg)}
            elif cmd == "set_config":
                apply_config_dict(d.cfg, msg["config"])
                d.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us:
                # a hard kill gives the brain no chance to write its safe states
                # or close its tasks (gotcha #25). Setting _stop ends
                # serve_forever, whose finally: stop() shuts the brain down.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError, TimeoutError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"
                    if isinstance(exc, KeyError) else f"{cmd}: {exc}"}

    def _info(self) -> dict:
        d = self.gen
        lay = d.layout
        from ..config import AI_CHANNELS, DIO_LINES
        return {
            "idn": d.status().idn,
            "device": d.cfg.hardware.device,
            "ai": [AI_CHANNELS[i] for i in lay.ai],
            "di": [DIO_LINES[i] for i in lay.di],
            "do": [DIO_LINES[i] for i in lay.do],
            "ao_limits_V": [list(d.ao_limits(i)) for i in (0, 1)],
            "ai_rate_Hz": d.cfg.ai.rate_Hz,
            "ai_samples_per_read": d.cfg.ai.samples_per_read,
            "config_path": d.config_path or "",
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
