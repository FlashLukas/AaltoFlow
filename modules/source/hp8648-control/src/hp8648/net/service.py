"""The service: wrap a SignalSource and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the brain. Besides the
brain's own worker thread (which owns the GPIB bus) it runs two threads:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies. Never allowed to die.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import spec
from ..source import SignalSource
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Hp8648Service:
    def __init__(self, source: SignalSource,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.src = source
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._stopped = False
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
        # route brain events into the publisher queue (thread-safe)
        self.src._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.src.start()
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
        print(f"hp8648 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the threads, then switch the RF off and disconnect."""
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        for t in (getattr(self, "_pub_t", None), getattr(self, "_cmd_t", None)):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        self.src.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.src.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self) -> int:
        """Current manifest revision. Cheap (a dozen dicts and a CRC), so it is
        recomputed for every frame: the power ceiling moves the moment the
        frequency crosses 2500 MHz, and a cached value would lag behind it."""
        return build_manifest(self.src)["revision"]

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
                except Exception:                       # never let the loop die
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
        if not isinstance(msg, dict):
            return {"ok": False, "error": "a command must be a JSON object"}
        cmd = msg.get("cmd")
        try:
            if cmd == "set_rf":
                self.src.set_rf(_bool(msg["on"]))
            elif cmd == "set_power":
                self.src.set_power(float(msg["power_dBm"]))
            elif cmd == "set_frequency":
                self.src.set_frequency(float(msg["frequency_Hz"]))
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.src)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.src.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.src.cfg, msg["config"])
                self.src.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill
                # us (gotcha #25): serve_forever's finally: stop() switches the
                # RF off and closes the instrument.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.src.cfg.limits
        hw = self.src.cfg.hardware
        st = self.src.status()
        return {
            "idn": st.idn,
            "model": "HP 8648D",
            # the EFFECTIVE limits (envelope inside the instrument's range),
            # the same numbers describe publishes
            "freq_min_Hz": self.src.freq_limits()[0],
            "freq_max_Hz": self.src.freq_limits()[1],
            "power_min_dBm": self.src.power_floor(),
            "power_max_dBm": lim.power_max_dBm,
            "power_ceiling_dBm": self.src.power_ceiling(),
            "enforce_spec_ceiling": lim.enforce_spec_ceiling,
            "option_1ea": hw.option_1ea,
            "freq_resolution_Hz": spec.FREQ_RESOLUTION_HZ,
            "power_resolution_dB": spec.POWER_RESOLUTION_DB,
        }


def _bool(v) -> bool:
    """A bool from JSON, or from a hand-typed "off" (gotcha #3)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
