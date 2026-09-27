"""The service: wrap a Synthesizer and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the Synthesizer. It runs
two extra threads, like every service in the suite:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards synthesizer events as they happen (one socket,
                  because a ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the synthesizer, and replies.

(The synthesizer has a third thread of its own, the worker that owns the serial
port -- see synthesizer.py.)

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.

Commands (a reply means ACCEPTED, not done -- poll status for the effect):
    set_rf        {channel: "a"|"b", on: bool}
    set_frequency {channel, frequency_Hz}
    set_power     {channel, power_dBm}
    set_phase     {channel, phase_deg}
    set_reference {source: "internal_10MHz"|"internal_27MHz"|"external", ext_MHz?}
    set_ext_ref   {ext_MHz}
    all_rf_off    {}
  + the universal verbs status, info, get_config, set_config, describe, shutdown.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..synthesizer import Synthesizer, CHANNELS
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class WindfreakService:
    def __init__(self, synth: Synthesizer,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.synth = synth
        self._rev = 0
        self._rev_at = -1e9
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._stopped = False

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # route synthesizer events into the publisher queue
        self.synth._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.synth.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"windfreak service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
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
        for t in (getattr(self, "_pub_t", None), getattr(self, "_cmd_t", None)):
            if t is not None:
                t.join(timeout=2.0)
        self.synth.shutdown()             # both outputs OFF, then disconnect

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = _nan_to_none(status_to_dict(self.synth.status()))
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.synth)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        pub = self._ctx.socket(zmq.PUB)
        pub.bind(self.pub_addr)
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
        rep = self._ctx.socket(zmq.REP)
        rep.bind(self.cmd_addr)
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
        s = self.synth
        try:
            if cmd == "set_rf":
                s.set_rf(msg["channel"], _as_bool(msg["on"]))
            elif cmd == "set_frequency":
                s.set_frequency(msg["channel"], float(msg["frequency_Hz"]))
            elif cmd == "set_power":
                s.set_power(msg["channel"], float(msg["power_dBm"]))
            elif cmd == "set_phase":
                s.set_phase(msg["channel"], float(msg["phase_deg"]))
            elif cmd == "set_reference":
                ext = msg.get("ext_MHz")
                s.set_reference(str(msg["source"]), None if ext is None else float(ext))
            elif cmd == "set_ext_ref":
                s.set_ext_ref(float(msg["ext_MHz"]))
            elif cmd == "all_rf_off":
                s.all_rf_off()
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(s)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(s.cfg)}
            elif cmd == "set_config":
                apply_config_dict(s.cfg, msg["config"])
                s.apply_config()
                self._rev_at = -1e9       # limits may have moved: recompute now
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to switch the RF off
                # (docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop ends
                # serve_forever, whose finally: stop() shuts the brain down.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            if cmd in ("set_reference", "set_ext_ref"):
                self._rev_at = -1e9       # the manifest's SHAPE follows the reference
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.synth.cfg.limits
        st = self.synth.status()
        return {
            "idn": st.get("idn", ""),
            "channels": list(CHANNELS),
            "freq_min_Hz": lim.freq_min_Hz,
            "freq_max_Hz": lim.freq_max_Hz,
            "power_min_dBm": lim.power_min_dBm,
            "power_max_dBm": lim.power_max_dBm,
            "phase_min_deg": lim.phase_min_deg,
            "phase_max_deg": lim.phase_max_deg,
            "ext_ref_min_MHz": lim.ext_ref_min_MHz,
            "ext_ref_max_MHz": lim.ext_ref_max_MHz,
        }


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    # NaN (no temperature yet) is not valid JSON for strict readers: send null
    return json.dumps(_nan_to_none(d)).encode("utf-8")


def _nan_to_none(d: dict) -> dict:
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in d.items()}
