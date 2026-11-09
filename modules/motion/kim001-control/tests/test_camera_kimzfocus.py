"""The camera drives this service with its EXISTING kim Z client, unchanged.

camera-control/src/camera/backends/remote_kim.py (KimLink + KimZFocus) is the
client that focuses the camera through a kim service. This module keeps kim's
wire contract so the same client works on a KIM001 -- only the module key
(keyring lookup) and the ports differ. These tests start the REAL kim001
service (simulator) on test ports 19470/19471 and drive it with a minimal copy
of that client's request/response pattern (raw pyzmq + json, like the camera:
no kim001 import on the client side), exercising every call KimZFocus makes:

  status / fresh_status (REQ) and the PUB status cache (SUB, topic b"status"),
  move_to_um axis Z (one step size) and move_to_step axis Z (two sizes:
  the camera plans the step target itself), the "target" in the reply,
  wait_settled on moving[2] + position_steps[2] == target,
  z_range from limit_lo/hi[2] x um_per_step[2], resolution,
  set_calibration axis Z direction +1 / -1, save_calibration -> "path",
  zero_counter axis Z, hw_error, and an {"ok": false} reply -> an exception.

It is a copy rather than an import so the two projects stay decoupled (the
suite's rule); if remote_kim.py changes how it talks, update this copy.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")

from kim001.config import Config  # noqa: E402
from kim001.control import make_identity  # noqa: E402
from kim001.net.service import Kim001Service  # noqa: E402
from kim001.sim_system import build_sim_system  # noqa: E402

CMD, PUB = 19470, 19471


# --------------------------------------------------------------------------- #
# the minimal KimLink: REQ for commands, SUB cache for status
# --------------------------------------------------------------------------- #
class MiniKimLink:
    def __init__(self, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000):
        self.ctx = zmq.Context.instance()
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self.req.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect(f"tcp://{host}:{cmd_port}")
        # the camera is a MACHINE client: it keeps moving while a person's GUI
        # holds control
        self.identity = make_identity("machine", "camera")
        self._cache, self._cache_t = None, 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._addr = f"tcp://{host}:{pub_port}"
        self._th = threading.Thread(target=self._sub_loop, daemon=True)
        self._th.start()

    def _sub_loop(self):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.RCVTIMEO, 200)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(self._addr)
        sub.setsockopt(zmq.SUBSCRIBE, b"status")
        try:
            while not self._stop.is_set():
                try:
                    _topic, payload = sub.recv_multipart()
                except zmq.Again:
                    continue
                with self._lock:
                    self._cache = json.loads(payload.decode("utf-8"))
                    self._cache_t = time.monotonic()
        finally:
            sub.close(0)

    def close(self):
        self._stop.set()
        self._th.join(timeout=2)
        self.req.close(0)

    def rpc(self, **req):
        req.setdefault("client", self.identity)
        self.req.send_json(req)
        reply = self.req.recv_json()
        if not reply.get("ok", False):
            raise RuntimeError(f"kim {req.get('cmd')}: {reply.get('error', 'failed')}")
        return reply

    def fresh_status(self):
        st = self.rpc(cmd="status")["status"]
        with self._lock:
            self._cache, self._cache_t = st, time.monotonic()
        return st

    def status(self, stale_s=1.0):
        with self._lock:
            st, t = self._cache, self._cache_t
        if st is not None and time.monotonic() - t <= stale_s:
            return st
        return self.fresh_status()


def _position(st, key):
    if st.get("hw_error"):
        raise RuntimeError(f"kim: {st['hw_error']}")
    return st[key]


class _DirectionalCounter:
    """camera backends/base.py DirectionalCounter, condensed."""

    def __init__(self):
        self._anchor = None

    def reset(self):
        self._anchor = None

    def position(self, steps, up, down, mean):
        sizes = (float(up), float(down), float(mean))
        a = self._anchor
        if a is None or a[2] != sizes:
            a = self._anchor = (float(steps), float(steps) * sizes[2], sizes)
        dn = float(steps) - a[0]
        return a[1] + dn * (sizes[0] if dn > 0 else sizes[1])

    def plan(self, steps_now, target, up, down, mean):
        here = self.position(steps_now, up, down, mean)
        self._anchor = (float(steps_now), here, (float(up), float(down), float(mean)))
        d = float(target) - here
        size = float(up) if d > 0 else float(down)
        return float(steps_now) + (d / size if size > 0 else 0.0)


class MiniKimZFocus:
    """The calls camera's KimZFocus makes, in the same order and shape."""

    def __init__(self, link, settle_timeout_s=20.0):
        self.link = link
        self.settle_timeout_s = settle_timeout_s
        self._target_steps = None
        self._dc = _DirectionalCounter()

    @staticmethod
    def _sizes(st):
        mean = float((st.get("um_per_step") or [0.02] * 3)[2])
        up = float((st.get("um_per_step_fwd") or [mean] * 3)[2]) or mean
        down = float((st.get("um_per_step_bwd") or [mean] * 3)[2]) or mean
        return up, down, mean

    @staticmethod
    def _directional(sizes):
        up, down, _ = sizes
        return abs(up - down) > 1e-9 * max(abs(up), abs(down), 1e-12)

    def read_z(self):
        st = self.link.status()
        sizes = self._sizes(st)
        if not self._directional(sizes):
            return float(_position(st, "position_um")[2])
        return self._dc.position(int(_position(st, "position_steps")[2]), *sizes)

    def set_z(self, um):
        st = self.link.status()
        sizes = self._sizes(st)
        if not self._directional(sizes):
            reply = self.link.rpc(cmd="move_to_um", axis="Z", position=float(um))
        else:
            now = int(_position(self.link.fresh_status(), "position_steps")[2])
            target = self._dc.plan(now, float(um), *sizes)
            reply = self.link.rpc(cmd="move_to_step", axis="Z", position=int(round(target)))
        self._target_steps = int(reply["target"])

    def wait_settled(self):
        if self._target_steps is None:
            return
        t_end = time.monotonic() + self.settle_timeout_s
        while True:
            st = self.link.fresh_status()
            if not st["moving"][2] and int(st["position_steps"][2]) == self._target_steps:
                return
            if time.monotonic() > t_end:
                raise TimeoutError(f"Z did not reach {self._target_steps}")
            time.sleep(0.02)

    def z_range(self):
        st = self.link.status()
        ups = float(st["um_per_step"][2])
        return (float(st["limit_lo"][2]) * ups, float(st["limit_hi"][2]) * ups)

    def resolution(self):
        up, down, mean = self._sizes(self.link.status())
        return max(up, down) if self._directional((up, down, mean)) else mean

    def set_step_sizes(self, up, down):
        self.link.rpc(cmd="set_calibration", axis="Z", value=float(up), direction=1)
        self.link.rpc(cmd="set_calibration", axis="Z", value=float(down), direction=-1)
        self._dc.reset()
        self.link.fresh_status()

    def save_step_sizes(self):
        try:
            reply = self.link.rpc(cmd="save_calibration")
        except RuntimeError as exc:
            if "unknown" in str(exc).lower():
                return None
            raise
        return str(reply.get("path", ""))

    def zero_counter(self):
        self.link.rpc(cmd="zero_counter", axis="Z")
        self._dc.reset()
        self._target_steps = 0
        self.link.fresh_status()

    def counter_steps(self):
        return int(_position(self.link.status(), "position_steps")[2])

    def move_counter(self, steps):
        reply = self.link.rpc(cmd="move_to_step", axis="Z", position=int(round(steps)))
        self._target_steps = int(reply["target"])


# --------------------------------------------------------------------------- #
@pytest.fixture()
def focus(tmp_path):
    brain, backend = build_sim_system(Config())
    brain.config_path = tmp_path / "kim001.ini"
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=8)
    svc.start()
    link = MiniKimLink()
    z = MiniKimZFocus(link)
    try:
        link.rpc(cmd="set_step_rate", axis="Z", value=2000)    # keep the test quick
        yield z, brain, link
    finally:
        link.close()
        svc.stop()
        time.sleep(0.1)


def test_status_has_the_3_wide_shape_the_camera_reads(focus):
    z, brain, link = focus
    st = link.fresh_status()
    for key in ("position_steps", "position_um", "moving", "um_per_step",
                "um_per_step_fwd", "um_per_step_bwd", "limit_lo", "limit_hi",
                "target_um"):
        assert len(st[key]) == 3, key
    assert st["hw_error"] == ""
    assert st["moving"][:2] == [False, False] and st["position_steps"][:2] == [0, 0]
    # the PUB stream carries the same frames (the camera reads Z every frame)
    t_end = time.monotonic() + 3
    while link._cache_t == 0.0 and time.monotonic() < t_end:
        time.sleep(0.02)
    assert link._cache is not None and len(link._cache["position_um"]) == 3


def test_set_z_wait_settled_read_z_with_one_step_size(focus):
    z, brain, _link = focus
    assert z.z_range() == pytest.approx((-13000.0, 13000.0))
    assert z.resolution() == pytest.approx(0.02)
    z.set_z(12.5)                          # move_to_um axis Z -> 625 steps
    assert z._target_steps == 625
    z.wait_settled()
    assert z.read_z() == pytest.approx(12.5)
    assert brain.status().position_steps[2] == 625
    z.set_z(-3.0)                          # down: negative of the datum is fine
    z.wait_settled()
    assert z.read_z() == pytest.approx(-3.0)


def test_step_calibration_written_saved_and_used_by_the_camera(focus):
    """The camera's "Calibrate Z steps": set_calibration Z +1 / -1, then
    save_calibration. With two sizes the camera moves by STEP target."""
    z, brain, _link = focus
    z.set_step_sizes(0.015, 0.020)         # up smaller than down (as on the rig)
    assert brain.um_per_step(2, +1) == pytest.approx(0.015)
    assert brain.um_per_step(2, -1) == pytest.approx(0.020)
    path = z.save_step_sizes()
    assert path and path.endswith("kim001.ini")
    from kim001.config import load_config
    c = load_config(path).calibration
    assert (c.um_per_step_z, c.um_per_step_z_bwd) == pytest.approx((0.015, 0.020))
    assert z.resolution() == pytest.approx(0.020)

    z.set_z(3.0)                           # 3 um up = 200 steps of 15 nm
    z.wait_settled()
    assert brain.status().position_steps[2] == 200
    assert z.read_z() == pytest.approx(3.0)
    z.set_z(1.0)                           # 2 um down = 100 steps of 20 nm
    z.wait_settled()
    assert brain.status().position_steps[2] == 100
    assert z.read_z() == pytest.approx(1.0)


def test_datum_counter_moves_and_refusals(focus):
    z, brain, link = focus
    z.move_counter(400)
    z.wait_settled()
    assert z.counter_steps() == 400
    z.zero_counter()                       # datum: nothing moves, counter 0
    assert z.counter_steps() == 0 and brain.status().position_steps[2] == 0
    z.wait_settled()                       # target 0 = already there
    # an X command (what a kim XY stage client would send) is refused loudly
    with pytest.raises(RuntimeError, match="not fitted on a KIM001"):
        link.rpc(cmd="move_to_um", axis="X", position=1.0)
    with pytest.raises(RuntimeError, match="not fitted on a KIM001"):
        link.rpc(cmd="move_image_px", dx=1.0, dy=0.0)
    # a clamped target is what the reply says, so wait_settled cannot hang
    link.rpc(cmd="set_leash", enabled=True, leash_z=50)
    z.set_z(100.0)                         # 5000 steps, leash 50
    assert z._target_steps == 50
    z.wait_settled()
    assert z.counter_steps() == 50
