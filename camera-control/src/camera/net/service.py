"""CameraService -- owns the brain, serves commands, publishes status (§6).

Two daemon threads, each owning exactly one socket (a ZeroMQ socket must not be
shared across threads):

  * publisher : PUB socket.  Sends a status frame every 1/status_hz seconds and
                drains the event queue as events arrive.
  * commander : REP socket.  poll(200ms) -> recv_json -> _dispatch -> send_json,
                wrapped so a bad command can never kill the loop.

The brain's ``_on_event`` hook is redirected into a thread-safe queue so events
raised on the command thread reach the publisher thread cleanly.
"""

from __future__ import annotations

import base64
import json as _json_mod
import queue
import threading
import time

import zmq

from .describe import build_manifest
from ..camera import Camera, status_to_dict
from . import protocol as P


class CameraService:
    def __init__(
        self,
        brain: Camera,
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
        self.brain._on_event = lambda level, msg: self._events.put(
            {"level": level, "msg": msg}
        )
        self.brain.start()
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._publisher, name="camera-pub", daemon=True),
            threading.Thread(target=self._commander, name="camera-cmd", daemon=True),
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
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # ------------------------------------------------------------------ #
    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.brain.status())
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
                try:
                    while True:
                        evt = self._events.get_nowait()
                        sock.send_multipart([P.TOPIC_EVENT, _json(evt)])
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + period
                    try:
                        payload = self.status_payload()
                        sock.send_multipart([P.TOPIC_STATUS, _json(payload)])
                    except Exception:
                        pass
                time.sleep(0.005)
        finally:
            sock.close(0)

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
                    except Exception as exc:
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
    def _dispatch(self, req: dict) -> dict:
        cmd = (req or {}).get("cmd")
        b = self.brain

        # -- universal --------------------------------------------------- #
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
            lim = P.config_to_dict(b.cfg)["limits"]
            return {"ok": True, "info": {
                "idn": b.backend.idn(),
                "limits": lim,
                "objective": b.cfg.image.objective_name,
                "pixel_size_um": [b.cfg.image.pixel_size_x_um, b.cfg.image.pixel_size_y_um],
                "objectives": b.list_objectives(),
            }}
        if cmd == "get_config":
            return {"ok": True, "config": P.config_to_dict(b.cfg)}
        if cmd == "set_config":
            P.apply_config_dict(b.cfg, req.get("config", {}))
            b.apply_config()
            return {"ok": True}

        # -- focus / autofocus ------------------------------------------- #
        if cmd == "autofocus":
            return {"ok": True, "result": "queued", "af_id": b.autofocus()}
        if cmd == "kill_af":
            b.kill_af()
            return {"ok": True}
        if cmd == "set_continuous_focus":
            return {"ok": True, "on": b.set_continuous_focus(bool(req["on"]))}
        if cmd == "set_z":
            return {"ok": True, "z": b.set_z(req["volts"])}
        if cmd == "step_z":
            return {"ok": True, "z": b.step_z(req["delta"])}
        if cmd == "step_xy":
            return {"ok": True, "target": b.step_xy(req["dx"], req["dy"]),
                    "unit": b.xy_step_unit()}
        if cmd == "datum_xy":
            b.datum_xy()
            return {"ok": True}
        if cmd == "read_z":
            return {"ok": True, "z": b.read_z()}

        # -- tracking / stabiliser --------------------------------------- #
        if cmd == "set_tracking":
            return {"ok": True, "on": b.set_tracking(bool(req["on"]))}
        if cmd == "set_stabilize":
            return {"ok": True, "on": b.set_stabilize(bool(req["on"]))}
        if cmd == "set_selected_index":
            # either index may be omitted = keep it (a scan sweeps X and Y as
            # two separate axes, each sending only its own)
            if "ix" not in req and "iy" not in req:
                return {"ok": False, "error": "set_selected_index needs ix and/or iy"}
            return {"ok": True, "index": list(b.set_selected_index(req.get("ix"), req.get("iy")))}

        # -- motion / position ------------------------------------------- #
        if cmd == "move_xy":
            return {"ok": True, "target": b.move_xy(req["x"], req["y"])}
        if cmd == "read_xy":
            return {"ok": True, "xy": b.read_xy()}
        if cmd == "read_position_px":
            return {"ok": True, "position_px": b.read_position_px()}
        if cmd == "set_position_px":
            return {"ok": True, "target": b.set_position_px(req["x"], req["y"])}
        if cmd == "click_to_go":
            return {"ok": True, "target": b.click_to_go(req["px"], req["py"])}

        # -- template / reference ---------------------------------------- #
        if cmd == "capture_reference":
            roi = tuple(req["roi"])
            ac = tuple(req["array_center"]) if req.get("array_center") else None
            return {"ok": True, "reference": b.capture_reference(roi, ac)}
        if cmd == "capture_backup":
            return {"ok": True, "backup": b.capture_backup(tuple(req["roi"]))}
        if cmd == "clear_backups":
            b.clear_backups()
            return {"ok": True}
        if cmd == "list_backups":
            return {"ok": True, "backups": b.list_backups()}
        if cmd == "load_pattern":
            return {"ok": True, "reference": b.load_pattern(req["path"], bool(req.get("load_arrays", True)))}
        if cmd == "save_pattern":
            b.save_pattern(req["path"])
            return {"ok": True}

        # -- imaging ----------------------------------------------------- #
        if cmd == "snapshot":
            return {"ok": True, "path": b.snapshot(req.get("path"))}
        if cmd == "get_frame":
            png = b.get_frame_png()
            return {"ok": True, "png_b64": base64.b64encode(png).decode("ascii")}

        # -- calibration ------------------------------------------------- #
        if cmd == "set_objective":
            return {"ok": True, "objective": b.set_objective(req["name"])}
        if cmd == "list_objectives":
            return {"ok": True, "objectives": b.list_objectives()}

        # -- live camera parameters -------------------------------------- #
        if cmd == "camera_features":
            return {"ok": True, "features": b.camera_features()}
        if cmd == "get_camera_feature":
            return {"ok": True, "value": b.get_camera_feature(req["name"])}
        if cmd == "set_camera_feature":
            return {"ok": True, "value": b.set_camera_feature(req["name"], req["value"])}

        # -- scanning area / analysis ------------------------------------ #
        if cmd == "set_scan_area":
            return {"ok": True, "scan_area": b.set_scan_area(
                req["cx"], req["cy"], req["w"], req["h"], req.get("angle"))}
        if cmd == "set_scan_size":
            return {"ok": True, "pitch": b.set_scan_size_um(
                req["size_x_um"], req["size_y_um"])}
        if cmd == "get_scan_rect":
            return {"ok": True, "scan_rect": b.get_scan_rect()}
        if cmd == "get_af_curve":
            return {"ok": True, "curve": b.get_af_curve()}
        if cmd == "set_accuracy_logging":
            return {"ok": True, "on": b.set_accuracy_logging(bool(req["on"]))}
        if cmd == "get_accuracy":
            return {"ok": True, "accuracy": b.get_accuracy()}

        # -- spot position + config file ---------------------------------- #
        if cmd == "calibrate_spot":
            return {"ok": True, "spot": b.calibrate_spot(int(req.get("frames", 20)))}
        if cmd == "set_spot_position":
            return {"ok": True, "spot": b.set_spot_position(req["x"], req["y"])}
        if cmd == "clear_spot_position":
            b.clear_spot_position()
            return {"ok": True}
        if cmd == "save_config":
            return {"ok": True, "path": b.save_config(req.get("path"))}

        return {"ok": False, "error": f"unknown command {cmd!r}"}


def _json(obj) -> bytes:
    return _json_mod.dumps(obj).encode("utf-8")
