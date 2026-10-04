"""The service: wrap a LockIn and expose it over ZeroMQ.

One process owns the instrument (or the simulator). Two daemon threads, as in
every module: a publisher (owns PUB: status at `status_hz` + events) and a
commander (owns REP: one JSON command in, one reply out). The LockIn runs its
own third thread, the output poller (which also carries out the auto
operations); neither of these two ever touches the hardware directly except
through the brain's locked setters.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import tables
from ..control import ControlLease
from ..config import INPUT_MODES, REF_SOURCES
from ..lockin import LockIn
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, _json_safe)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Sr7230Service:
    def __init__(self, lockin: LockIn,
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
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. The lock-in drives the
        #   sample through OSC OUT (a modulation coil, a piezo, a laser
        #   driver), so the one "make it safe" action is taking the drive away:
        #   `output_off` (OSC OUT amplitude to 0 V). `set_amplitude` is NOT in
        #   the list even though it can go to 0 V -- it can turn the drive UP
        #   as well; that is why `output_off` is a verb of its own. `acquire`,
        #   `stream_start/stop` and the auto operations are not safety: they
        #   are triggers, and a new sample (or an auto sensitivity) changes
        #   what another client -- a running scan -- is waiting on.
        #   READ = read-only verbs whose names do not start with get_/read_/
        #   list_: `stream_read` only drains the recorded fly-scan readings.
        self.control = ControlLease(
            safety={"output_off"},
            read={"stream_read"},
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
        print(f"sr7230 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
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

    def _dispatch(self, msg: dict) -> dict:
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        li = self.lockin
        try:
            if cmd == "set_reference":
                li.set_reference(msg["source"])
            elif cmd == "set_frequency":
                li.set_frequency(msg["frequency_Hz"])
            elif cmd == "output_off":
                # the SAFETY verb: OSC OUT to 0 V -- a verb of its own so a
                # viewer may send it (it can only make things safer)
                li.output_off()
            elif cmd == "set_amplitude":
                li.set_amplitude(msg["amplitude_V"])
            elif cmd == "set_phase":
                li.set_phase(msg["phase_deg"])
            elif cmd == "set_harmonic":
                li.set_harmonic(msg["harmonic"])
            elif cmd == "set_input":
                li.set_input(msg["mode"])
            elif cmd == "set_coupling":
                li.set_coupling(msg["coupling"])
            elif cmd == "set_sensitivity":
                li.set_sensitivity(msg["sensitivity"])
            elif cmd == "set_full_scale":
                li.set_full_scale(msg["full_scale"])
            elif cmd == "set_time_constant":
                li.set_time_constant(msg["time_constant_s"])
            elif cmd == "set_slope":
                li.set_slope(msg["slope"])
            elif cmd == "set_fast_mode":
                li.set_fast_mode(msg["enabled"])
            elif cmd in ("auto_phase", "auto_sensitivity", "auto_measure"):
                # Queued; the poll thread runs it. The caller waits for status
                # to show THIS auto_id with auto_busy false.
                return {"ok": True, "auto_id": li.auto(cmd)}
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

    def _info(self) -> dict:
        li = self.lockin
        cfg = li.cfg
        lim = cfg.limits
        hw = cfg.hardware
        allowed = li.allowed_tcs()
        return {
            "idn": li.status().idn,
            "model": "Signal Recovery 7230",
            "address": f"{hw.host}:{hw.port}" if hw.host else "(simulated or not set)",
            "freq_min_Hz": lim.freq_min_Hz, "freq_max_Hz": li.freq_max_Hz(),
            "instrument_freq_max_Hz": li.instrument_f_max(),
            "tc_min_s": min(allowed), "tc_max_s": max(allowed),
            "amplitude_max_V": lim.amplitude_max_V,
            "sensitivities": [
                {"index": i, "full_scale": fs,
                 "label": tables.sensitivity_label(i, cfg.signal.input)}
                for i, fs in sorted(li.sensitivity_table().items())],
            "slopes_db": list(tables.allowed_slopes(cfg.filter.fast_mode)),
            "input_modes": list(INPUT_MODES),
            "ref_sources": list(REF_SOURCES),
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
