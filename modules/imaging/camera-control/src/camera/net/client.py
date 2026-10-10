"""CameraClient -- a brain-compatible facade over the socket (§6).

Presents the SAME method names as the :class:`camera.camera.Camera` brain, so the
GUI can drive a local brain or a remote service through identical calls -- only
the object it is handed changes.  A background SUB thread caches the newest status
(returned as a :class:`CameraStatus`, the very same dataclass the brain uses, so
GUI code is agnostic about local vs remote).

Commands go out on a REQ socket under a lock; a timed-out REQ socket is rebuilt
(the standard lazy-pirate recovery).
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import fields

import numpy as np

from ..camera import CameraStatus
from ..control import ControlClient
from .. import secure
from . import protocol as P

_FIELDS = {f.name for f in fields(CameraStatus)}


def _status_from_dict(d: dict) -> CameraStatus:
    return CameraStatus(**{k: v for k, v in (d or {}).items() if k in _FIELDS})


class CameraClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script, "machine" only for a
    program that must not be locked out (scan-core). A script must
    ``take_control()`` before it may change anything while a GUI holds control."""

    def __init__(self, host=P.DEFAULT_HOST, cmd_port=P.DEFAULT_CMD_PORT,
                 pub_port=P.DEFAULT_PUB_PORT, timeout_ms=3000,
                 kind="script", name="camera client"):
        self._control_setup(kind, name)
        import zmq
        self._zmq = zmq
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.timeout_ms = timeout_ms

        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self._make_req()

        self._status = CameraStatus()
        self._on_event = lambda level, msg: None
        self._stop = threading.Event()
        self._sub_thread = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        self._stop.clear()
        self._sub_thread = threading.Thread(target=self._sub_loop,
                                            name="camera-client-sub", daemon=True)
        self._sub_thread.start()
        self.start_heartbeat()           # "still here": counted as a viewer / keeps control
        try:
            self.info()
            self.get_config()
        except Exception:
            pass

    def close(self) -> None:
        self.stop_heartbeat()
        self._stop.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1.0)
        with self._lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    # -- REQ plumbing ------------------------------------------------------ #
    def _make_req(self) -> None:
        self._req = self._ctx.socket(self._zmq.REQ)
        self._req.setsockopt(self._zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(self._zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(self._zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures the camera (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "camera")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        self._with_identity(req)
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._req.send_json(req)
                    reply = self._req.recv_json()
                    break
                except self._zmq.Again:
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (started before the policy changed): the new socket
                    # tries that mode, once (secure.no_answer). Safe to resend:
                    # a request in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "camera")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"no reply to {req.get('cmd')} within {self.timeout_ms} ms") from None
        if not reply.get("ok", False):
            self._raise_refusal(reply)       # ControlRefused: another client has control
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # -- SUB caching thread ------------------------------------------------ #
    def _sub_loop(self) -> None:
        def make_sub():
            s = self._ctx.socket(self._zmq.SUB)
            secure.secure_client(s, self.host, "camera")   # telemetry too
            s.connect(f"tcp://{self.host}:{self.pub_port}")
            s.setsockopt(self._zmq.SUBSCRIBE, b"")
            return s, secure.flip_generation()

        sub, gen = make_sub()
        poller = self._zmq.Poller()
        poller.register(sub, self._zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the service in the other mode: follow it
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = make_sub()
                    poller.register(sub, self._zmq.POLLIN)
                if dict(poller.poll(200)):
                    # One malformed frame (not two parts, not JSON, not a dict)
                    # must not end this thread: it used to raise out of the
                    # loop, the daemon thread died silently and the GUI showed
                    # the last status forever (deep cleaning 2026-09-28).
                    try:
                        topic, raw = sub.recv_multipart()
                        payload = json.loads(raw.decode("utf-8"))
                        if not isinstance(payload, dict):
                            continue
                    except Exception:
                        continue
                    if topic == P.TOPIC_STATUS:
                        self._status = _status_from_dict(payload)
                        self._control_from_status(payload)
                    elif topic == P.TOPIC_EVENT:
                        try:
                            self._on_event(payload.get("level", "info"), payload.get("msg", ""))
                        except Exception:
                            pass
        finally:
            sub.close(0)

    # -- brain-compatible surface ----------------------------------------- #
    def describe(self) -> dict:
        """The service's parameter manifest: controls, indicators and actions.

        Limits inside it are LIVE, not constants, so compare
        `status().describe_rev` against the manifest's `revision` rather than
        caching this forever. See `net/describe.py`.
        """
        r = self._rpc(cmd="describe")
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> CameraStatus:
        return self._status

    def info(self) -> dict:
        return self._rpc(cmd="info")["info"]

    def get_config(self) -> dict:
        return self._rpc(cmd="get_config")["config"]

    def set_config(self, config: dict) -> None:
        self._rpc(cmd="set_config", config=config)

    # focus
    def autofocus(self) -> int:
        """Queue an autofocus; returns its number (status ``af_id``), like the brain."""
        return self._rpc(cmd="autofocus")["af_id"]

    def kill_af(self) -> None:
        self._rpc(cmd="kill_af")

    def calibrate_z_steps(self) -> int:
        """Queue a Z step calibration; returns its number (status ``zcal_id``)."""
        return self._rpc(cmd="calibrate_z_steps")["zcal_id"]

    def get_zcal_curve(self) -> dict:
        return self._rpc(cmd="get_zcal_curve").get("curve", {})

    def set_continuous_focus(self, on: bool) -> bool:
        return self._rpc(cmd="set_continuous_focus", on=bool(on))["on"]

    def set_z(self, volts: float) -> float:
        return self._rpc(cmd="set_z", volts=volts)["z"]

    def step_z(self, delta: float) -> float:
        return self._rpc(cmd="step_z", delta=delta)["z"]

    def step_xy(self, dx: float, dy: float) -> list:
        return self._rpc(cmd="step_xy", dx=dx, dy=dy)["target"]

    def datum_xy(self) -> None:
        self._rpc(cmd="datum_xy")

    def datum_z(self) -> None:
        self._rpc(cmd="datum_z")

    def save_scan_pattern(self, folder: str = "", name: str = "") -> dict:
        rep = self._rpc(cmd="save_scan_pattern", folder=folder, name=name)
        return {"path": rep.get("path"), "info": rep.get("info")}

    def save_picture(self, folder: str = "", name: str = "") -> dict:
        rep = self._rpc(cmd="save_picture", folder=folder, name=name)
        return {"path": rep.get("path"), "info": rep.get("info")}

    def reconnect_stage(self) -> dict:
        rep = self._rpc(cmd="reconnect_stage")
        return {"stage_ok": rep.get("stage_ok", True), "stage_error": rep.get("stage_error", "")}

    def stage_state(self) -> tuple:
        st = self.status()
        return bool(getattr(st, "stage_ok", True)), getattr(st, "stage_error", "")

    def read_z(self) -> float:
        return self._rpc(cmd="read_z")["z"]

    # tracking / stabiliser
    def set_tracking(self, on: bool) -> bool:
        return self._rpc(cmd="set_tracking", on=bool(on))["on"]

    def clear_fault(self) -> str:
        return self._rpc(cmd="clear_fault").get("result", "")

    def set_stabilize(self, on: bool) -> bool:
        return self._rpc(cmd="set_stabilize", on=bool(on))["on"]

    def set_selected_index(self, ix: int, iy: int) -> list:
        return self._rpc(cmd="set_selected_index", ix=ix, iy=iy)["index"]

    # the laser on the sample (um from the main template)
    def set_laser_target(self, x: float | None = None, y: float | None = None) -> list:
        msg = {"cmd": "set_laser_target"}
        if x is not None:
            msg["x"] = float(x)
        if y is not None:
            msg["y"] = float(y)
        return self._rpc(**msg)["target"]

    def cancel_laser_target(self) -> None:
        self._rpc(cmd="cancel_laser_target")

    # motion / position
    def move_xy(self, x: float, y: float) -> list:
        return self._rpc(cmd="move_xy", x=x, y=y)["target"]

    def read_xy(self) -> list:
        return self._rpc(cmd="read_xy")["xy"]

    def read_position_px(self) -> dict:
        return self._rpc(cmd="read_position_px")["position_px"]

    def set_position_px(self, x: float, y: float) -> list:
        return self._rpc(cmd="set_position_px", x=x, y=y)["target"]

    def click_to_go(self, px: float, py: float) -> list:
        return self._rpc(cmd="click_to_go", px=px, py=py)["target"]

    # template
    def capture_reference(self, roi, array_center=None) -> str:
        return self._rpc(cmd="capture_reference", roi=list(roi),
                         array_center=list(array_center) if array_center else None)["reference"]

    def capture_backup(self, roi) -> str:
        return self._rpc(cmd="capture_backup", roi=list(roi))["backup"]

    def clear_backups(self) -> None:
        self._rpc(cmd="clear_backups")

    def list_backups(self) -> list:
        return self._rpc(cmd="list_backups")["backups"]

    def load_pattern(self, path: str, load_arrays: bool = True) -> str:
        return self._rpc(cmd="load_pattern", path=path, load_arrays=load_arrays)["reference"]

    def save_pattern(self, path: str) -> None:
        self._rpc(cmd="save_pattern", path=path)

    # imaging
    def snapshot(self, path: str | None = None) -> str:
        return self._rpc(cmd="snapshot", path=path)["path"]

    def acquire_image(self) -> int:
        """Ask for a fresh frame for recording; returns its number (the frame
        is taken by the service's frame loop: wait for status image_id == n
        and not image_acquiring, then get_image())."""
        return int(self._rpc(cmd="acquire_image")["image_id"])

    def get_image(self, which: str = "sample") -> tuple:
        """(meta, frame) of the last acquired image ("sample") or of the
        current frame ("live"), at full depth, cropped/binned as configured.
        JSON + base64 here (this client's REQ speaks one-part replies);
        scan-core asks for the same frame as a binary part."""
        r = self._rpc(cmd="get_image", which=which)
        img = r["image"]
        frame = np.frombuffer(base64.b64decode(img["b64"]),
                              dtype=np.dtype(img["dtype"])).reshape(img["shape"])
        return r.get("image_meta", {}), frame.copy()

    def get_frame(self) -> "np.ndarray | None":
        import cv2
        png_b64 = self._rpc(cmd="get_frame")["png_b64"]
        if not png_b64:
            return None
        buf = np.frombuffer(base64.b64decode(png_b64), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)

    # calibration
    def set_objective(self, name: str) -> dict:
        return self._rpc(cmd="set_objective", name=name)["objective"]

    def list_objectives(self) -> list:
        return self._rpc(cmd="list_objectives")["objectives"]

    def store_objective_af(self, key: str, value=None, previous=None) -> dict:
        rep = self._rpc(cmd="store_objective_af", key=key, value=value, previous=previous)
        return {k: rep.get(k) for k in ("objective", "key", "value", "path")}

    # live camera parameters
    def calibrate_spot(self, frames: int = 20) -> dict:
        return self._rpc(cmd="calibrate_spot", frames=frames)["spot"]

    def auto_exposure_once(self) -> dict:
        return self._rpc(cmd="auto_exposure_once")["exposure"]

    def set_spot_position(self, x: float, y: float) -> dict:
        return self._rpc(cmd="set_spot_position", x=x, y=y)["spot"]

    def clear_spot_position(self) -> None:
        self._rpc(cmd="clear_spot_position")

    def save_config(self, path: str | None = None) -> str:
        return self._rpc(cmd="save_config", path=path)["path"]

    def camera_features(self) -> list:
        return self._rpc(cmd="camera_features")["features"]

    def get_camera_feature(self, name: str):
        return self._rpc(cmd="get_camera_feature", name=name)["value"]

    def set_camera_feature(self, name: str, value):
        return self._rpc(cmd="set_camera_feature", name=name, value=value)["value"]

    # scanning area / analysis
    def set_scan_area(self, cx: float, cy: float, w: float, h: float,
                      angle: float | None = None) -> dict:
        return self._rpc(cmd="set_scan_area", cx=cx, cy=cy, w=w, h=h,
                         angle=angle)["scan_area"]

    def set_scan_size_um(self, size_x_um: float, size_y_um: float) -> dict:
        return self._rpc(cmd="set_scan_size", size_x_um=size_x_um,
                         size_y_um=size_y_um)["pitch"]

    def get_scan_rect(self) -> dict:
        return self._rpc(cmd="get_scan_rect")["scan_rect"]

    def get_af_curve(self) -> dict:
        return self._rpc(cmd="get_af_curve")["curve"]

    def set_accuracy_logging(self, on: bool) -> bool:
        return self._rpc(cmd="set_accuracy_logging", on=bool(on))["on"]

    def get_accuracy(self) -> dict:
        return self._rpc(cmd="get_accuracy")["accuracy"]
