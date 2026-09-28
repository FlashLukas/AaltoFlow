"""The service: wrap a DspLockIn and expose it over ZeroMQ.

One process owns the instrument (or the simulator). Two daemon threads, as in
every module: a publisher (owns PUB: status at `status_hz` + events) and a
commander (owns REP: one JSON command in, one reply out). The brain runs its
own third thread, the GPIB poller; neither of these two ever touches the
hardware directly except through the brain's locked setters.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..lockin import DspLockIn
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, _json_safe)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Sr830Service:
    def __init__(self, lockin: DspLockIn,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.lockin = lockin
        self._rev = 0
        self._rev_at = -1e9
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()

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
        self.lockin._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.lockin.start()
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
        # ASCII only: mission-control reads this through a pipe (gotcha #14)
        print(f"sr830 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        time.sleep(max(self.status_dt, 0.2) + 0.1)   # let both loops notice and close
        self.lockin.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for the PUB frame and the
        `status` reply, so the two can never drift apart."""
        st = status_to_dict(self.lockin.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Manifest revision, recomputed at most every `max_age_s`."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.lockin)["revision"]
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
                except Exception as exc:                     # keep publishing
                    self._events.put({"level": "error", "msg": f"status failed: {exc}"})
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
                except Exception as exc:                     # never let the loop die
                    try:
                        rep.send_json({"ok": False, "error": str(exc)})
                    except zmq.ZMQError:
                        pass
        # linger, not 0: after `shutdown` the reply may still be queued, and
        # close(0) would drop it -- the launcher would then kill us anyway.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    #: verb -> (brain method name, argument key). One table instead of a
    #: dozen elif branches: a new setter is one line here plus one in describe.
    _SETTERS = {
        "set_reference_source": ("set_reference_source", "source"),
        "set_frequency": ("set_frequency", "frequency_Hz"),
        "set_harmonic": ("set_harmonic", "harmonic"),
        "set_phase": ("set_phase", "phase_deg"),
        "set_trigger": ("set_trigger", "trigger"),
        "set_sine_out": ("set_sine_out", "sine_out_V"),
        "set_input_source": ("set_input_source", "source"),
        "set_input_ground": ("set_input_ground", "ground"),
        "set_input_coupling": ("set_input_coupling", "coupling"),
        "set_line_filter": ("set_line_filter", "line_filter"),
        "set_sensitivity": ("set_sensitivity", "sensitivity"),
        "set_reserve": ("set_reserve", "reserve"),
        "set_time_constant": ("set_time_constant", "time_constant"),
        "set_slope": ("set_slope", "slope"),
        "set_sync_filter": ("set_sync_filter", "enabled"),
    }

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        li = self.lockin
        try:
            if cmd in self._SETTERS:
                method, arg = self._SETTERS[cmd]
                getattr(li, method)(msg[arg])
            elif cmd == "set_aux_out":
                li.set_aux_out(msg["channel"], msg["volts"])
            elif cmd in ("auto_gain", "auto_reserve", "auto_phase"):
                # Returns at once with a run number; the caller waits for status
                # to show auto_id == that number with auto_busy false.
                return {"ok": True, "auto_id": getattr(li, cmd)()}
            elif cmd == "acquire":
                # Returns at once with the id. The caller waits for status to
                # show THIS id with acquiring == false.
                return {"ok": True, "acq_id": li.acquire()}
            elif cmd == "stream_start":
                # A fly scan: record every reading from now on (stream.py)
                return {"ok": True, "stream_id": li.stream.start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": li.stream.read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": li.stream.stop()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": _json_safe(li.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(li)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(li.cfg)}
            elif cmd == "set_config":
                apply_config_dict(li.cfg, msg["config"])
                li.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us:
                # it gives the brain the chance to make SINE OUT / AUX OUT safe and
                # hand the front panel back (gotcha #25). Setting _stop ends
                # serve_forever, whose finally: stop() shuts the brain down.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        from .. import tables
        cfg = self.lockin.cfg
        lim = cfg.limits
        return {
            "idn": self.lockin.status().idn,
            "resource": cfg.hardware.resource,
            "freq_min_Hz": lim.freq_min_Hz, "freq_max_Hz": lim.freq_max_Hz,
            "sine_min_V": lim.sine_min_V, "sine_max_V": lim.sine_max_V,
            "aux_out_min_V": lim.aux_out_min_V, "aux_out_max_V": lim.aux_out_max_V,
            "harmonic_max": lim.harmonic_max,
            "time_constants": list(tables.TC_LABELS),
            "sensitivities": list(tables.SENS_LABELS_V),
            "slopes": list(tables.SLOPES),
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
