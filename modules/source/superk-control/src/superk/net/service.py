"""The service: wrap the SuperK brain and expose it over ZeroMQ.

One process owns the laser (or the simulator) and the brain. Besides the
brain's own poll thread it runs two threads:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies. The loop never dies: any exception
                  becomes {"ok": false, "error": ...}.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network.

CLASS 4 LASER: `stop()` (reached from Ctrl-C, the `shutdown` verb, or any
exception in serve_forever) always runs the brain's shutdown, which switches RF
and emission OFF before disconnecting. A HARD kill runs no code at all; for that
case the brain arms the laser's own watchdog (hardware.watchdog_s).

LOST CLIENT: a message may carry "client": <id> -- today the control identity
{"id", "kind", "name", "host"} (control.py), whose "id" is used; a bare id
string from an older client still works. Every message is passed to
`laser.touch(id)` BEFORE it is dispatched (the control gate's `heartbeat`
included), so any command -- or the plain
`ping` verb the remote GUI's client sends every second -- counts as a
heartbeat. `set_emission{on: true, owner: <id>}` makes that client the owner;
if the owner goes silent for hardware.client_timeout_s, the brain switches
emission off. scan-core and the console send no owner, so a scan is never cut
(see CLAUDE.local.md for the trade-off).
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..control import ControlLease
from ..laser import SuperK
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class SuperkService:
    def __init__(self, laser: SuperK,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.laser = laser
        self._rev = 0
        self._rev_at = -1e9
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._stopped = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For a class 4 laser the
        #   one "make it safe" action is `emission_off` (the GUI's Emission
        #   OFF): a viewer who sees the beam where it should not be must be
        #   able to switch it off. `set_emission` is NOT in the list even
        #   though on=false is the same thing -- the same verb also switches
        #   the emission ON. (`set_rf` off only darkens the AOTF output while
        #   the laser keeps lasing, and the same verb switches it on: not
        #   safety either.)
        #   READ: `ping` only says "this client is alive" (the lost-client
        #   guard); it changes nothing.
        self.control = ControlLease(
            safety={"emission_off"},
            read={"ping"},
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
        self.laser._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.laser.start()
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
        try:
            self.start()
            print(f"superk service up - commands {self.cmd_addr} - status {self.pub_addr}")
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        time.sleep(self.status_dt + 0.1)
        self.laser.shutdown()            # RF off, emission off, disconnect

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place, for the publisher AND the
        `status` reply: a field in only one of them vanishes intermittently
        (a client uses the REQ path until its first PUB frame arrives)."""
        st = status_to_dict(self.laser.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`
        (it follows the active crystal's wavelength range)."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.laser)["revision"]
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
                try:
                    pub.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                except Exception:                  # never let the publisher die
                    pass
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
        cmd = msg.get("cmd")
        L = self.laser
        # heartbeat for the lost-client guard -- BEFORE the control gate, so a
        # refused command and the gate's own `heartbeat` count as "alive" too
        L.touch(_client_id(msg))
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        try:
            if cmd == "ping":
                return {"ok": True}
            elif cmd == "set_emission":
                L.set_emission(_bool(msg["on"]), owner=msg.get("owner"))
            elif cmd == "emission_on":           # describe action (danger)
                L.set_emission(True, owner=msg.get("owner"))
            elif cmd == "emission_off":          # describe action; the SAFETY verb
                L.emission_off()
            elif cmd == "reset_interlock":
                L.reset_interlock()
            elif cmd == "set_power":
                L.set_power(float(msg["power_pct"]))
            elif cmd == "set_rf":
                L.set_rf(_bool(msg["on"]))
            elif cmd == "set_filter":
                L.set_filter(msg["filter"])
            elif cmd == "set_wavelength":
                L.set_wavelength(int(msg.get("line", 1)), float(msg["wavelength_nm"]))
            elif cmd == "set_amplitude":
                L.set_amplitude(int(msg.get("line", 1)), float(msg["amplitude_pct"]))
            elif cmd == "set_line":
                L.set_line(int(msg.get("line", 1)), float(msg["wavelength_nm"]),
                           float(msg["amplitude_pct"]))
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(L)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(L.cfg)}
            elif cmd == "set_config":
                apply_config_dict(L.cfg, msg["config"])
                L.apply_config()
                self._rev_at = -1e9              # limits may have moved
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill
                # us (gotcha #25). serve_forever's finally: stop() then switches
                # RF and emission off and closes the port.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            if cmd in ("set_filter",):
                self._rev_at = -1e9              # the wavelength range moved
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            # SafetyError is a ValueError: a refused emission lands here too
            return {"ok": False, "error": f"{cmd}: {exc}"}

    def _info(self) -> dict:
        lim = self.laser.cfg.limits
        lo, hi = self.laser.wavelength_range()
        return {
            "idn": self.laser.status().idn,
            "filters": self.laser.filter_names(),
            "filter": self.laser.active_filter(),
            "wavelength_min_nm": lo,
            "wavelength_max_nm": hi,
            "power_min_pct": lim.power_min_pct,
            "power_max_pct": lim.power_max_pct,
            "amplitude_max_pct": lim.amplitude_max_pct,
            "lines": 8,
        }


def _bool(v) -> bool:
    """A JSON bool, or a string like "off" from a hand-typed command. bool("off")
    would be True (gotcha #3), so strings are parsed."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _client_id(msg: dict) -> str | None:
    """The sender's id for the lost-client guard: the control identity's
    "id" (control.py), or the bare id string older clients sent."""
    c = msg.get("client") if isinstance(msg, dict) else None
    if isinstance(c, dict):
        return str(c.get("id") or "") or None
    return c if isinstance(c, str) and c else None


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
