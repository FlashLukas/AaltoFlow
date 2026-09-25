"""KimClient -- a brain-compatible facade over the socket (§6 of the guide).

The client presents the SAME method names as the :class:`Kim` brain, so the GUI
can drive a local brain or a remote service through identical calls -- only the
object it is handed changes.  A background SUB thread caches the newest status
so ``status()`` is instant and never blocks on the network.

Commands go out on a REQ socket under a lock.  If a reply times out
(``zmq.Again``) the REQ socket is in a broken state, so we close and rebuild it
(the standard lazy-pirate recovery).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import zmq

from . import protocol as P


@dataclass
class RemoteStatus:
    """Mirror of :class:`kim.kim.KimStatus`, built from the wire dict.

    Same attribute names as the brain's status so GUI code is agnostic about
    local vs remote.
    """

    position_steps: list
    position_um: list
    rel_steps: list
    rel_um: list
    rel_origin: list
    moving: list
    step_rate: list
    velocity_um: list
    acceleration: list
    voltage: list
    um_per_step: list
    limit_lo: list
    limit_hi: list
    leash: bool
    leash_half: list
    speed_fast: bool
    step_large: bool
    connected: bool
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None
    px_calibrated: bool = False
    calib_running: bool = False
    calib_progress: str = ""
    #: step size per direction and where it comes from ("camera" | "config");
    #: default to the mean/datasheet so an older service still drives this client
    um_per_step_fwd: list = field(default_factory=lambda: [0.02, 0.02, 0.02])
    um_per_step_bwd: list = field(default_factory=lambda: [0.02, 0.02, 0.02])
    um_per_step_src: list = field(default_factory=lambda: ["config"] * 3)


def _status_from_dict(d: dict) -> RemoteStatus:
    return RemoteStatus(
        describe_rev=d.get("describe_rev"),
        position_steps=d.get("position_steps", [0, 0, 0]),
        position_um=d.get("position_um", [0.0, 0.0, 0.0]),
        rel_steps=d.get("rel_steps", [0, 0, 0]),
        rel_um=d.get("rel_um", [0.0, 0.0, 0.0]),
        rel_origin=d.get("rel_origin", [0, 0, 0]),
        moving=d.get("moving", [False, False, False]),
        step_rate=d.get("step_rate", [0.0, 0.0, 0.0]),
        velocity_um=d.get("velocity_um", [0.0, 0.0, 0.0]),
        acceleration=d.get("acceleration", [0.0, 0.0, 0.0]),
        voltage=d.get("voltage", [0.0, 0.0, 0.0]),
        um_per_step=d.get("um_per_step", [0.02, 0.02, 0.02]),
        um_per_step_fwd=d.get("um_per_step_fwd", d.get("um_per_step", [0.02] * 3)),
        um_per_step_bwd=d.get("um_per_step_bwd", d.get("um_per_step", [0.02] * 3)),
        um_per_step_src=d.get("um_per_step_src", ["config"] * 3),
        limit_lo=d.get("limit_lo", [0, 0, 0]),
        limit_hi=d.get("limit_hi", [1_250_000, 1_250_000, 1_250_000]),
        leash=d.get("leash", False),
        leash_half=d.get("leash_half", [50000, 50000, 50000]),
        speed_fast=d.get("speed_fast", True),
        step_large=d.get("step_large", True),
        connected=d.get("connected", False),
        px_calibrated=d.get("px_calibrated", False),
        calib_running=d.get("calib_running", False),
        calib_progress=d.get("calib_progress", ""),
    )


class KimClient:
    def __init__(
        self,
        host: str = P.DEFAULT_HOST,
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        timeout_ms: int = 2000,
    ):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.timeout_ms = timeout_ms

        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self._make_req()

        # Latest cached status + last event, updated by the SUB thread.
        self._status = _status_from_dict({})
        self._on_event = lambda level, msg: None
        self._stop = threading.Event()
        self._sub_thread = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Begin caching status and fetch initial info + config."""
        self._stop.clear()
        self._sub_thread = threading.Thread(
            target=self._sub_loop, name="kim-client-sub", daemon=True
        )
        self._sub_thread.start()
        # Prime info/config so callers can rely on them right after start().
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

    # ------------------------------------------------------------------ #
    # REQ plumbing
    # ------------------------------------------------------------------ #
    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        with self._lock:
            try:
                self._req.send_json(req)
                reply = self._req.recv_json()
            except zmq.Again:
                # Timed out: REQ socket is stuck mid-transaction -> rebuild it.
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"no reply to {req.get('cmd')} within {self.timeout_ms} ms")
        if not reply.get("ok", False):
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # ------------------------------------------------------------------ #
    # SUB caching thread
    # ------------------------------------------------------------------ #
    def _sub_loop(self) -> None:
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")  # all topics
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    import json
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

    # ------------------------------------------------------------------ #
    # brain-compatible surface
    # ------------------------------------------------------------------ #
    def describe(self) -> dict:
        """The service's parameter manifest: controls, indicators and actions.

        Limits inside it are LIVE, not constants, so compare
        `status().describe_rev` against the manifest's `revision` rather than
        caching this forever. See `net/describe.py`.
        """
        r = self._rpc(cmd="describe")
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        return self._status

    def info(self) -> dict:
        return self._rpc(cmd="info")["info"]

    def get_config(self) -> dict:
        return self._rpc(cmd="get_config")["config"]

    def set_config(self, config: dict) -> None:
        self._rpc(cmd="set_config", config=config)

    # motion -- STEP language
    def move_to_step(self, axis, position) -> int:
        return self._rpc(cmd="move_to_step", axis=axis, position=position)["target"]

    def move_steps(self, axis, delta) -> int:
        return self._rpc(cmd="move_steps", axis=axis, delta=delta)["target"]

    # motion -- MICROMETRE language
    def move_to_um(self, axis, position) -> int:
        return self._rpc(cmd="move_to_um", axis=axis, position=position)["target"]

    def move_relative_um(self, axis, delta) -> int:
        return self._rpc(cmd="move_relative_um", axis=axis, delta=delta)["target"]

    def stop(self, axis=None) -> None:
        self._rpc(cmd="stop", axis=axis)

    def stop_all(self) -> None:
        self._rpc(cmd="stop", axis=None)

    # datum + display origin
    def zero_counter(self, axis=None) -> None:
        self._rpc(cmd="zero_counter", axis=axis)

    def zero_counter_all(self) -> None:
        self._rpc(cmd="zero_counter", axis=None)

    def set_zero(self, axis=None) -> list:
        return self._rpc(cmd="set_zero", axis=axis)["rel_origin"]

    def set_zero_all(self) -> list:
        return self._rpc(cmd="set_zero", axis=None)["rel_origin"]

    def clear_zero(self, axis=None) -> None:
        self._rpc(cmd="clear_zero", axis=axis)

    # parameters
    def set_step_rate(self, axis, value) -> float:
        return self._rpc(cmd="set_step_rate", axis=axis, value=value)["value"]

    def set_velocity_um(self, axis, value) -> float:
        return self._rpc(cmd="set_velocity_um", axis=axis, value=value)["value"]

    def set_acceleration(self, axis, value) -> float:
        return self._rpc(cmd="set_acceleration", axis=axis, value=value)["value"]

    def set_voltage(self, axis, value) -> float:
        return self._rpc(cmd="set_voltage", axis=axis, value=value)["value"]

    def set_calibration(self, axis, value, direction: int = 0) -> float:
        """um per step. direction 0 = both ways, +1 forward only, -1 backward only."""
        return self._rpc(cmd="set_calibration", axis=axis, value=value,
                         direction=int(direction))["value"]

    def set_leash(self, enabled=None, leash_xy=None, leash_z=None) -> dict:
        return self._rpc(
            cmd="set_leash", enabled=enabled, leash_xy=leash_xy, leash_z=leash_z
        )["leash"]

    def set_speed(self, fast: bool) -> dict:
        return self._rpc(cmd="set_speed", fast=bool(fast))["speed"]

    def set_step_size(self, large: bool) -> dict:
        return self._rpc(cmd="set_step_size", large=bool(large))["step"]

    # camera-frame calibration (image px per step)
    def start_px_calibration(self, camera_host="127.0.0.1", camera_port=5563,
                             voltages=(85, 95, 105, 115, 125), repeats=2) -> str:
        return self._rpc(cmd="start_px_calibration", camera_host=camera_host,
                         camera_port=camera_port, voltages=list(voltages),
                         repeats=repeats)["state"]

    def abort_px_calibration(self) -> None:
        self._rpc(cmd="abort_px_calibration")

    def get_px_calibration(self) -> dict | None:
        return self._rpc(cmd="get_px_calibration")["calibration"]

    def move_image_px(self, dx, dy, context=None) -> list:
        return self._rpc(cmd="move_image_px", dx=dx, dy=dy, context=context)["steps"]

    # position list
    def store_position(self, slot, name="") -> dict:
        return self._rpc(cmd="store_position", slot=slot, name=name)["position"]

    def clear_position(self, slot) -> None:
        self._rpc(cmd="clear_position", slot=slot)

    def goto_position(self, slot) -> list:
        return self._rpc(cmd="goto_position", slot=slot)["targets"]

    def get_positions(self) -> list:
        return self._rpc(cmd="get_positions")["positions"]

    def save_positions(self, path) -> None:
        self._rpc(cmd="save_positions", path=path)

    def load_positions(self, path) -> list:
        return self._rpc(cmd="load_positions", path=path)["positions"]
