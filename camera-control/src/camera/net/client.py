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
from . import protocol as P

_FIELDS = {f.name for f in fields(CameraStatus)}


def _status_from_dict(d: dict) -> CameraStatus:
    return CameraStatus(**{k: v for k, v in (d or {}).items() if k in _FIELDS})


class CameraClient:
    def __init__(self, host=P.DEFAULT_HOST, cmd_port=P.DEFAULT_CMD_PORT,
                 pub_port=P.DEFAULT_PUB_PORT, timeout_ms=3000):
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
        try:
            self.info()
            self.get_config()
        except Exception:
            pass

    def close(self) -> None:
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
        self._req.setsockopt(self._zmq.LINGER, 0)
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        with self._lock:
            try:
                self._req.send_json(req)
                reply = self._req.recv_json()
            except self._zmq.Again:
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"no reply to {req.get('cmd')} within {self.timeout_ms} ms")
        if not reply.get("ok", False):
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # -- SUB caching thread ------------------------------------------------ #
    def _sub_loop(self) -> None:
        sub = self._ctx.socket(self._zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(self._zmq.SUBSCRIBE, b"")
        poller = self._zmq.Poller()
        poller.register(sub, self._zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    payload = json.loads(raw.decode("utf-8"))
                    if topic == P.TOPIC_STATUS:
                        self._status = _status_from_dict(payload)
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

    def read_z(self) -> float:
        return self._rpc(cmd="read_z")["z"]

    # tracking / stabiliser
    def set_tracking(self, on: bool) -> bool:
        return self._rpc(cmd="set_tracking", on=bool(on))["on"]

    def set_stabilize(self, on: bool) -> bool:
        return self._rpc(cmd="set_stabilize", on=bool(on))["on"]

    def set_selected_index(self, ix: int, iy: int) -> list:
        return self._rpc(cmd="set_selected_index", ix=ix, iy=iy)["index"]

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

    # live camera parameters
    def calibrate_spot(self, frames: int = 20) -> dict:
        return self._rpc(cmd="calibrate_spot", frames=frames)["spot"]

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
