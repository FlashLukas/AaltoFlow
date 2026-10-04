"""AgilisClient -- a brain-compatible facade over the socket (section 6 of the guide).

The client presents the SAME method names as the :class:`AgilisStage` brain, so the GUI
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

from .. import secure
from ..control import ControlClient
from . import protocol as P


@dataclass
class RemoteStatus:
    """Mirror of :class:`agilis.agilis.AgilisStatus`, built from the wire dict.

    Same attribute names as the brain's status so GUI code is agnostic about
    local vs remote. Defaults describe "nothing known yet" (not connected).
    """

    position_steps: list = field(default_factory=lambda: [0, 0])
    position_um: list = field(default_factory=lambda: [0.0, 0.0])
    rel_steps: list = field(default_factory=lambda: [0, 0])
    rel_um: list = field(default_factory=lambda: [0.0, 0.0])
    rel_origin: list = field(default_factory=lambda: [0, 0])
    target_steps: list = field(default_factory=lambda: [0, 0])
    target_um: list = field(default_factory=lambda: [0.0, 0.0])
    moving: list = field(default_factory=lambda: [False, False])
    axis_state: list = field(default_factory=lambda: [0, 0])
    jogging: list = field(default_factory=lambda: [False, False])
    direction: list = field(default_factory=lambda: [0, 0])
    amplitude_fwd: list = field(default_factory=lambda: [16, 16])
    amplitude_bwd: list = field(default_factory=lambda: [16, 16])
    um_per_step: list = field(default_factory=lambda: [0.05, 0.05])
    um_per_step_fwd: list = field(default_factory=lambda: [0.05, 0.05])
    um_per_step_bwd: list = field(default_factory=lambda: [0.05, 0.05])
    cal_amp_fwd: list = field(default_factory=lambda: [16, 16])
    cal_amp_bwd: list = field(default_factory=lambda: [16, 16])
    cal_valid: list = field(default_factory=lambda: [True, True])
    uncal_steps: list = field(default_factory=lambda: [0, 0])
    estimate_ok: list = field(default_factory=lambda: [True, True])
    limit_lo: list = field(default_factory=lambda: [-240_000, -240_000])
    limit_hi: list = field(default_factory=lambda: [240_000, 240_000])
    limit_switch: list = field(default_factory=lambda: [False, False])
    leash: bool = False
    leash_steps: int = 20000
    step_large: bool = False
    connected: bool = False
    hw_error: str = ""
    poll_hz: float = 0.0
    travel_um: float = 12000.0
    measured_um: list = field(default_factory=lambda: [None, None])
    measured_steps: list = field(default_factory=lambda: [None, None])
    routine: str = ""
    routine_id: int = 0
    routine_running: bool = False
    routine_error: str = ""
    routine_msg: str = ""
    usb_busy: bool = False
    startup_writes: list = field(default_factory=list)
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None


_FIELDS = set(RemoteStatus.__dataclass_fields__)


def _status_from_dict(d: dict) -> RemoteStatus:
    """Unknown keys are ignored and missing ones keep their default, so a
    newer or older service still drives this client."""
    return RemoteStatus(**{k: v for k, v in (d or {}).items() if k in _FIELDS})


class AgilisClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core). While a GUI on
    another PC holds control, a script must ``take_control()`` before it may
    change anything; a refused command raises ``ControlRefused``."""

    def __init__(
        self,
        host: str = P.DEFAULT_HOST,
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        timeout_ms: int = 2000,
        kind: str = "script",
        name: str = "agilis client",
    ):
        self._control_setup(kind, name)
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
            target=self._sub_loop, name="agilis-client-sub", daemon=True
        )
        self._sub_thread.start()
        self.start_heartbeat()           # "still here": counted as a viewer / keeps control
        # Prime info/config so callers can rely on them right after start().
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

    # ------------------------------------------------------------------ #
    # REQ plumbing
    # ------------------------------------------------------------------ #
    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures agilis (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "agilis")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        self._with_identity(req)         # say who we are (control.py)
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._req.send_json(req)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # Timed out: REQ socket is stuck mid-transaction -> rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "agilis")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"no reply to {req.get('cmd')} within {self.timeout_ms} ms") from None
        if not reply.get("ok", False):
            self._raise_refusal(reply)       # ControlRefused: another client has control
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # ------------------------------------------------------------------ #
    # SUB caching thread
    # ------------------------------------------------------------------ #
    def _sub_loop(self) -> None:
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            secure.secure_client(s, self.host, "agilis")      # telemetry too
            s.connect(f"tcp://{self.host}:{self.pub_port}")
            s.setsockopt(zmq.SUBSCRIBE, b"")  # all topics
            return s, secure.flip_generation()

        sub, gen = make_sub()
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the service in the other mode
                    # (secure.no_answer): telemetry follows
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = make_sub()
                    poller.register(sub, zmq.POLLIN)
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    import json
                    payload = json.loads(raw.decode("utf-8"))
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

    def jog(self, axis, speed) -> int:
        """Start or keep alive a continuous jog (-4..4); repeat it while held."""
        return self._rpc(cmd="jog", axis=axis, speed=int(speed))["mode"]

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

    # amplitude + calibration
    def set_amplitude(self, axis, value, direction: int = 0) -> int:
        """Step amplitude 1..50. direction 0 = both, +1 forward, -1 backward."""
        return self._rpc(cmd="set_amplitude", axis=axis, value=value,
                         direction=int(direction))["value"]

    def set_step_size(self, large: bool) -> dict:
        return self._rpc(cmd="set_step_size", large=bool(large))["step"]

    def set_calibration(self, axis, value, direction: int = 0) -> float:
        """um per step. direction 0 = both ways, +1 forward only, -1 backward only."""
        return self._rpc(cmd="set_calibration", axis=axis, value=value,
                         direction=int(direction))["value"]

    def set_leash(self, enabled=None, leash_steps=None) -> dict:
        return self._rpc(cmd="set_leash", enabled=enabled, leash_steps=leash_steps)["leash"]

    # fly-scan stream
    def stream_start(self, rate_hz=None) -> int:
        return self._rpc(cmd="stream_start", rate_hz=rate_hz)["stream_id"]

    def stream_read(self) -> dict:
        return self._rpc(cmd="stream_read")["stream"]

    def stream_stop(self) -> dict:
        return self._rpc(cmd="stream_stop")["stream"]

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

    # -- limit-switch stage (AG-LS25) ------------------------------------ #
    def move_to_limit(self, axis, direction, speed=3) -> int:
        return self._rpc(cmd="move_to_limit", axis=axis, direction=direction,
                         speed=speed)["mode"]

    def measure_position(self, axis) -> int:
        return self._rpc(cmd="measure_position", axis=axis)["routine_id"]

    def move_absolute(self, axis, position_um) -> int:
        return self._rpc(cmd="move_absolute", axis=axis, position=position_um)["routine_id"]

    def measure_step_size(self, axis) -> int:
        return self._rpc(cmd="measure_step_size", axis=axis)["routine_id"]
