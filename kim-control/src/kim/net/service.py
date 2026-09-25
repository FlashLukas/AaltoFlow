"""KimService -- owns the brain, serves commands, publishes status (§6).

Two daemon threads, each owning exactly one socket (a ZeroMQ socket must not be
shared across threads):

  * publisher : PUB socket.  Sends a status frame every 1/status_hz seconds and
                drains the event queue as events arrive.
  * commander : REP socket.  poll(200ms) -> recv_json -> _dispatch -> send_json.
                Wrapped so a bad command can never kill the loop.

The brain's ``_on_event`` hook is redirected into a thread-safe queue so events
raised on the command thread reach the publisher thread cleanly.
"""

from __future__ import annotations

import queue
import threading
import time

import zmq

from ..kim import Kim
from .describe import build_manifest
from . import protocol as P


class KimService:
    def __init__(
        self,
        brain: Kim,
        host: str = "0.0.0.0",
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        status_hz: float = 8.0,
    ):
        self.brain = brain
        self._rev = 0
        self._rev_at = 0.0
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.status_hz = status_hz

        self._ctx = zmq.Context.instance()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        # Route brain events into our queue (they get PUBlished as b"event").
        self.brain._on_event = lambda level, msg: self._events.put(
            {"level": level, "msg": msg}
        )
        self.brain.start()

        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._publisher, name="kim-pub", daemon=True),
            threading.Thread(target=self._commander, name="kim-cmd", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        try:
            self.brain.shutdown()
        except Exception:
            pass

    def serve_forever(self) -> None:
        """Convenience: start and block until Ctrl-C."""
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # ------------------------------------------------------------------ #
    # publisher thread
    # ------------------------------------------------------------------ #
    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = P.status_to_dict(self.brain.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale. Rebuilding the
        manifest at the status rate would be pure waste.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.brain)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        sock = self._ctx.socket(zmq.PUB)
        sock.bind(f"tcp://{self.host}:{self.pub_port}")
        period = 1.0 / self.status_hz
        next_status = time.monotonic()
        try:
            while not self._stop.is_set():
                # 1) forward any pending events immediately
                try:
                    while True:
                        evt = self._events.get_nowait()
                        sock.send_multipart([P.TOPIC_EVENT, _json(evt)])
                except queue.Empty:
                    pass

                # 2) publish status on schedule
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + period
                    try:
                        payload = self.status_payload()
                        sock.send_multipart([P.TOPIC_STATUS, _json(payload)])
                    except Exception:
                        pass  # status must never take the publisher down

                time.sleep(0.005)
        finally:
            sock.close(0)

    # ------------------------------------------------------------------ #
    # commander thread
    # ------------------------------------------------------------------ #
    def _commander(self) -> None:
        sock = self._ctx.socket(zmq.REP)
        sock.bind(f"tcp://{self.host}:{self.cmd_port}")
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    try:
                        req = sock.recv_json()
                    except Exception:
                        continue
                    try:
                        reply = self._dispatch(req)
                    except Exception as exc:  # never die on a bad command
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    try:
                        sock.send_json(reply)
                    except Exception:
                        pass
        finally:
            # linger, not 0: after `shutdown` the reply may still be queued, and
            # close(0) would drop it -- the launcher would then kill us anyway.
            sock.close(linger=500)

    # ------------------------------------------------------------------ #
    # command dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, req: dict) -> dict:
        cmd = (req or {}).get("cmd")
        b = self.brain

        # -- universal commands (every module implements these) ---------- #
        if cmd == "status":
            return {"ok": True, "status": self.status_payload()}
        if cmd == "describe":
            return {"ok": True, "describe": build_manifest(self.brain)}
        if cmd == "shutdown":
            # A CLEAN stop, asked for by the launcher before it would kill us: a
            # hard kill gives the brain no chance to close its hardware (it wedged
            # the PM16 until replugged, docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop
            # ends serve_forever, whose finally: stop() shuts the brain down.
            self._stop.set()
            return {"ok": True, "stopping": True}
        if cmd == "info":
            return {
                "ok": True,
                "info": {
                    "idn": b.backend.idn(),
                    "axes": list(("X", "Y", "Z")),
                    "limits": P.config_to_dict(b.cfg)["limits"],
                    "calibration": P.config_to_dict(b.cfg)["calibration"],
                    "n_slots": len(b.positions.slots),
                },
            }
        if cmd == "get_config":
            return {"ok": True, "config": P.config_to_dict(b.cfg)}
        if cmd == "set_config":
            P.apply_config_dict(b.cfg, req.get("config", {}))
            b.apply_config()
            return {"ok": True}

        # -- motion: STEP language --------------------------------------- #
        if cmd == "move_to_step":
            target = b.move_to_step(P.parse_axis(req["axis"]), req["position"])
            return {"ok": True, "target": target}
        if cmd == "move_steps":
            target = b.move_steps(P.parse_axis(req["axis"]), req["delta"])
            return {"ok": True, "target": target}

        # -- motion: MICROMETRE language --------------------------------- #
        if cmd == "move_to_um":
            target = b.move_to_um(P.parse_axis(req["axis"]), req["position"])
            return {"ok": True, "target": target}
        if cmd == "move_relative_um":
            target = b.move_relative_um(P.parse_axis(req["axis"]), req["delta"])
            return {"ok": True, "target": target}

        if cmd == "stop":
            if req.get("axis") is None:
                b.stop_all()
            else:
                b.stop(P.parse_axis(req["axis"]))
            return {"ok": True}

        # -- datum + display origin -------------------------------------- #
        if cmd == "zero_counter":
            if req.get("axis") is None:
                b.zero_counter_all()
            else:
                b.zero_counter(P.parse_axis(req["axis"]))
            return {"ok": True}
        if cmd == "set_zero":
            if req.get("axis") is None:
                origins = b.set_zero_all()
            else:
                origins = [b.set_zero(P.parse_axis(req["axis"]))]
            return {"ok": True, "rel_origin": origins}
        if cmd == "clear_zero":
            if req.get("axis") is None:
                b.clear_zero_all()
            else:
                b.clear_zero(P.parse_axis(req["axis"]))
            return {"ok": True}

        # -- parameters -------------------------------------------------- #
        if cmd == "set_step_rate":
            v = b.set_step_rate(P.parse_axis(req["axis"]), req["value"])
            return {"ok": True, "value": v}
        if cmd == "set_velocity_um":
            v = b.set_velocity_um(P.parse_axis(req["axis"]), req["value"])
            return {"ok": True, "value": v}
        if cmd == "set_acceleration":
            a = b.set_acceleration(P.parse_axis(req["axis"]), req["value"])
            return {"ok": True, "value": a}
        if cmd == "set_voltage":
            v = b.set_voltage(P.parse_axis(req["axis"]), req["value"])
            return {"ok": True, "value": v}
        if cmd == "set_calibration":
            # direction: 0/absent = both ways, +1 forward only, -1 backward only
            v = b.set_calibration(P.parse_axis(req["axis"]), req["value"],
                                  int(req.get("direction", 0)))
            return {"ok": True, "value": v}
        if cmd == "set_leash":
            state = b.set_leash(
                enabled=req.get("enabled"),
                leash_xy=req.get("leash_xy"),
                leash_z=req.get("leash_z"),
            )
            return {"ok": True, "leash": state}
        if cmd == "set_speed":
            return {"ok": True, "speed": b.set_speed(bool(req["fast"]))}
        if cmd == "set_step_size":
            return {"ok": True, "step": b.set_step_size(bool(req["large"]))}

        # -- camera-frame calibration (image px per step) ------------------ #
        if cmd == "start_px_calibration":
            opts = {k: req[k] for k in ("camera_host", "camera_port", "voltages", "repeats")
                    if req.get(k) is not None}
            return {"ok": True, "state": b.start_px_calibration(**opts)}
        if cmd == "abort_px_calibration":
            b.abort_px_calibration()
            return {"ok": True}
        if cmd == "get_px_calibration":
            return {"ok": True, "calibration": b.get_px_calibration()}
        if cmd == "move_image_px":
            steps = b.move_image_px(req["dx"], req["dy"], context=req.get("context"))
            return {"ok": True, "steps": steps}

        # -- position list ----------------------------------------------- #
        if cmd == "store_position":
            p = b.store_position(int(req["slot"]), req.get("name", ""))
            return {"ok": True, "position": p}
        if cmd == "clear_position":
            b.clear_position(int(req["slot"]))
            return {"ok": True}
        if cmd == "goto_position":
            targets = b.goto_position(int(req["slot"]))
            return {"ok": True, "targets": targets}
        if cmd == "get_positions":
            return {"ok": True, "positions": b.get_positions()}
        if cmd == "save_positions":
            b.save_positions(req["path"])
            return {"ok": True}
        if cmd == "load_positions":
            b.load_positions(req["path"])
            return {"ok": True, "positions": b.get_positions()}

        return {"ok": False, "error": f"unknown command {cmd!r}"}


# zmq's send_json is fine, but events go out on a raw multipart frame, so we
# serialise those ourselves with the same encoder.
import json as _json_mod


def _json(obj) -> bytes:
    return _json_mod.dumps(obj).encode("utf-8")
