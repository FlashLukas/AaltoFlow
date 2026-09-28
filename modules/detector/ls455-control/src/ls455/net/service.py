"""The service: wrap a Gaussmeter and expose it over ZeroMQ.

One process owns the meter (one GPIB address / one COM port can only have one
owner). Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The Gaussmeter's own polling thread does the actual reading.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..gaussmeter import Gaussmeter
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Ls455Service:
    def __init__(self, meter: Gaussmeter,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.meter = meter
        self._rev = 0
        self._rev_at = 0.0
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
        self.meter._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.meter.start()
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
        print(f"ls455 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.meter.shutdown()
        print("ls455 service stopped, meter closed")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.meter.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes when auto-range or the mode is toggled (controls appear /
        disappear) and when the probe's range list changes."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.meter)["revision"]
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
                    msg = rep.recv_json()
                    rep.send_json(self._dispatch(msg))
                except Exception as exc:                       # never let the loop die
                    try:
                        rep.send_json({"ok": False, "error": str(exc)})
                    except zmq.ZMQError:
                        pass
        # linger, not 0: after `shutdown` the reply may still be in the queue,
        # and close(0) would throw it away -- the launcher would then wait for
        # nothing and fall back to killing us.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        m = self.meter
        try:
            if cmd == "set_mode":
                m.set_mode(str(msg["mode"]))
            elif cmd == "set_dc_digits":
                m.set_dc_digits(int(msg["digits"]))
            elif cmd == "set_rms_band":
                m.set_rms_band(str(msg["band"]))
            elif cmd == "set_auto_range":
                m.set_auto_range(bool(msg["on"]))
            elif cmd == "set_range":
                m.set_range(float(msg["range_mT"]))
            elif cmd == "set_display_unit":
                m.set_display_unit(str(msg["unit"]))
            elif cmd == "set_relative":
                # either argument may come alone: the describe control for the
                # setpoint sends only `setpoint_mT`, the checkbox only `on`
                if "on" not in msg and "setpoint_mT" not in msg:
                    raise KeyError("on or setpoint_mT")
                on = bool(msg["on"]) if "on" in msg else m.cfg.meter.relative
                sp = msg.get("setpoint_mT")
                m.set_relative(on, None if sp is None else float(sp))
            elif cmd == "relative_here":
                m.relative_here()
            elif cmd == "set_acquisition":
                m.set_acquisition(int(msg["readings"]))
            elif cmd == "zero":
                m.zero()
            elif cmd == "clear_zero":
                m.clear_zero()
            elif cmd == "reread_probe":
                # synchronous: the reply comes after the probe was re-read, so
                # describe's `wait: immediate` is the truth
                m.reread_probe()
            elif cmd == "acquire":
                return {"ok": True, "acq_id": m.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(m.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(m)}
            elif cmd == "info":
                return {"ok": True, "info": json_safe(self._info())}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(m.cfg)}
            elif cmd == "set_config":
                apply_config_dict(m.cfg, msg["config"])
                m.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (gotcha #25). A hard kill mid-query can leave a GPIB bus or a
                # serial port in a state the next open trips over.
                # Setting _stop ends serve_forever, which closes the meter; the
                # reply still goes out, because the commander sends it before it
                # looks at _stop again.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        st = self.meter.status()
        return {
            "idn": st.idn, "probe": st.probe, "probe_serial": st.probe_serial,
            "probe_type_code": st.probe_type_code,
            "probe_sensitivity_mV_per_kG": st.probe_sensitivity_mV_per_kG,
            "probe_geometry": st.probe_geometry, "probe_desc": st.probe_desc,
            "ranges_mT": st.ranges_mT,
            "range_min_mT": st.range_min_mT, "range_max_mT": st.range_max_mT,
            "unit": "mT",
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
