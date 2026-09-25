"""KimXYStage + KimZFocus -- drive XY and Z through the kim-control SERVICE.

On the lab rig (2026-09-13) the sample sits on a KIM101 + 3x PIA25 inertia
stage: X = ch1, Y = ch2, Z = ch3. kim-control owns that controller; the camera
is a CLIENT of it, exactly as it is of piezo-control on the piezo rig. Like
remote_xy.py this speaks the RAW protocol (pyzmq + json, no ``kim`` import),
so the two projects stay decoupled. Channel mapping is kim's business: we only
ever say "X", "Y" or "Z".

What is different from the piezo rig, and why this file looks the way it does:

* ONE service, TWO backends. XY and Z share one ``KimLink`` (one REQ socket,
  one SUB cache), so the camera holds one connection to kim, not two.

* Read from the PUB stream, not by asking. The camera engine reads XY position
  and "moving" EVERY frame (~20 fps). Each kim ``status`` request costs six
  queries on the KIM101's single USB link, so polling would add ~250 queries/s
  to a link that already jammed once under load. kim publishes status at 8 Hz
  anyway; we cache it and only fall back to a request if the cache goes stale.

* The stage owns its limits (``owns_limits``). KIM positions are centred on the
  Datum and go negative; the camera's own 0..130 um piezo envelope would clamp
  a correction to -3 um into a jump to 0. The brain asks us for the LIVE range
  instead -- kim's effective limits, i.e. the leash when it is armed.

* Z is in MICROMETRES (``z_unit``), not volts, and it is OPEN-LOOP
  (``open_loop``): it walks at the step rate rather than jumping, so autofocus
  must wait for arrival (``wait_settled``) and approach each level from one
  direction (see Autofocus.approach_margin).

Positions are the kim step COUNTER x um_per_step: commanded, not measured.
The stabiliser does not care (the image closes the loop), but an absolute um
readout is only as good as kim's unmeasured calibration.
"""

from __future__ import annotations

import json
import threading
import time

import zmq

AXES = ("X", "Y", "Z")


class KimLink:
    """One shared connection to kim-control: REQ for commands, SUB for status."""

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5567,
                 pub_port: int = 5568, timeout_ms: int = 1500,
                 stale_s: float = 1.0, retry_s: float = 10.0):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.timeout_ms = timeout_ms
        self.stale_s = stale_s
        # After a status request times out, report "unreachable" at once for
        # this long instead of waiting out the timeout again. The camera engine
        # reads stage status several times PER FRAME; without this, kim being
        # down froze the image (0 frames in 3 s, found 2026-09-13). Recovery
        # does not wait for the retry: kim's first PUB frame refills the cache,
        # and a fresh cache is served before this check is even reached.
        self.retry_s = retry_s
        self._down_until = 0.0
        self._ctx = zmq.Context.instance()
        self._req_lock = threading.Lock()
        self._req = None
        self._cache_lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_t = 0.0
        self._users = 0               # XY and Z both open/close the link
        self._stop = threading.Event()
        self._sub_thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        with self._req_lock:
            self._users += 1
            if self._users > 1:
                return
            self._make_req()
        self._stop.clear()
        self._sub_thread = threading.Thread(target=self._sub_loop, name="kim-sub",
                                            daemon=True)
        self._sub_thread.start()

    def close(self) -> None:
        with self._req_lock:
            self._users = max(0, self._users - 1)
            if self._users:
                return
            if self._req is not None:
                self._req.close(0)
                self._req = None
        self._stop.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1.0)
            self._sub_thread = None

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _sub_loop(self) -> None:
        # A ZeroMQ socket must stay in the thread that uses it, so the SUB
        # socket is created and closed here.
        sub = self._ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.RCVTIMEO, 200)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"status")
        try:
            while not self._stop.is_set():
                try:
                    _topic, payload = sub.recv_multipart()
                except zmq.Again:
                    continue
                except Exception:
                    continue
                try:
                    st = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                with self._cache_lock:
                    self._cache, self._cache_t = st, time.monotonic()
                # kim is talking again: commands may go through at once
                self._down_until = 0.0
        finally:
            sub.close(0)

    # -- is kim there? ----------------------------------------------------- #
    #: kim publishes status at ~8 Hz; this long without a frame means it is gone.
    ALIVE_S = 2.0

    def available(self) -> tuple:
        """(True, "") while kim's status stream is live, else (False, why).

        Read from the SUB cache only -- never a request -- so the camera can ask
        every frame and a GUI can grey out the stage controls without anything
        waiting on a timeout.
        """
        now = time.monotonic()
        with self._cache_lock:
            have, t = self._cache is not None, self._cache_t
        if have and now - t <= self.ALIVE_S:
            return True, ""
        where = f"{self.host}:{self.cmd_port}"
        if not have:
            return False, f"stage (kim service at {where}) has not answered yet"
        return False, f"stage (kim service at {where}) silent for {now - t:.0f} s"

    def reconnect(self) -> tuple:
        """Rebuild both sockets and ask kim once. Returns available()-style (ok, why).

        For the GUI's "Reconnect stage": after kim was restarted (or started
        after the camera), or when a link got stuck. Clears the "known down"
        state so the next command is really sent.
        """
        with self._req_lock:
            if self._req is not None:
                self._req.close(0)
            self._make_req()
        self._down_until = 0.0
        if self._users:                      # restart the status listener
            self._stop.set()
            if self._sub_thread is not None:
                self._sub_thread.join(timeout=1.0)
            self._stop.clear()
            self._sub_thread = threading.Thread(target=self._sub_loop, name="kim-sub",
                                                daemon=True)
            self._sub_thread.start()
        try:
            self.fresh_status()
        except Exception as exc:
            self._down_until = time.monotonic() + self.retry_s
            return False, f"stage still not answering: {exc}"
        return True, ""

    # -- requests ---------------------------------------------------------- #
    def rpc(self, **req) -> dict:
        """Send one command; raise on timeout or on an ``{"ok": false}`` reply."""
        with self._req_lock:
            # Known down (a timeout less than retry_s ago, and no status frame
            # since): fail NOW instead of waiting out another timeout. Checked
            # after taking the lock, so callers queued behind a timing-out
            # request do not each wait their own 1.5 s in turn.
            if time.monotonic() < self._down_until:
                raise ConnectionError("stage (kim service) not answering -- "
                                      "use 'Reconnect stage' once it runs")
            if self._req is None:
                self._make_req()
            try:
                self._req.send_json(req)
                reply = self._req.recv_json()
            except zmq.Again:
                # A REQ socket that timed out is stuck mid-exchange: rebuild it.
                self._req.close(0)
                self._make_req()
                self._down_until = time.monotonic() + self.retry_s
                raise TimeoutError(f"kim service did not answer {req.get('cmd')!r}")
        if not reply.get("ok", False):
            raise RuntimeError(f"kim {req.get('cmd')}: {reply.get('error', 'failed')}")
        return reply

    def fresh_status(self) -> dict:
        """A status read NOW (a request), for when a stale frame would lie."""
        st = self.rpc(cmd="status")["status"]
        with self._cache_lock:
            self._cache, self._cache_t = st, time.monotonic()
        return st

    def status(self) -> dict:
        """The latest published status; a request only if the cache is stale.

        Raises ConnectionError straight away while kim is known to be down
        (see ``retry_s``); a fresh PUB frame clears that immediately.
        """
        now = time.monotonic()
        with self._cache_lock:
            st, t = self._cache, self._cache_t
        if st is not None and now - t <= self.stale_s:
            return st
        if now < self._down_until:
            raise ConnectionError("kim service unreachable")
        try:
            return self.fresh_status()
        except TimeoutError:
            self._down_until = time.monotonic() + self.retry_s
            raise


def _axis_range_um(st: dict, axis: int) -> tuple:
    ups = float(st["um_per_step"][axis])
    return (float(st["limit_lo"][axis]) * ups, float(st["limit_hi"][axis]) * ups)


class KimXYStage:
    """XYStage over kim axes X and Y."""

    owns_limits = True    # clamp to kim's live range, not cfg.limits.motor_*
    open_loop = True      # slip-stick: don't average stabiliser frames mid-move

    def __init__(self, link: KimLink):
        self.link = link

    def open(self) -> None:
        self.link.open()

    def close(self) -> None:
        self.link.close()

    def available(self) -> tuple:
        return self.link.available()

    def reconnect(self) -> tuple:
        return self.link.reconnect()

    def read_xy(self) -> tuple:
        pos = self.link.status()["position_um"]
        return (float(pos[0]), float(pos[1]))

    def move_xy(self, x_um: float, y_um: float) -> None:
        # kim has no two-axis verb: two fire-and-forget moves, back to back.
        # The KIM101 drives one channel at a time anyway.
        self.link.rpc(cmd="move_to_um", axis="X", position=float(x_um))
        self.link.rpc(cmd="move_to_um", axis="Y", position=float(y_um))

    def moving(self) -> bool:
        mv = self.link.status()["moving"]
        return bool(mv[0] or mv[1])

    # -- native step language (jog + datum) -------------------------------- #
    # kim's um are step count x a NOMINAL um_per_step that has not been measured
    # on this rig, so jogging is offered in steps, the unit that is actually true.
    def read_steps(self) -> tuple:
        pos = self.link.status()["position_steps"]
        return (int(pos[0]), int(pos[1]))

    def steps_for_um(self, axis: int, um: float) -> int:
        """How many steps `um` is on this axis, IN THE DIRECTION it travels.

        kim publishes the step size of each direction (measured by the camera
        calibration, or typed in). A slip-stick actuator does not step equally
        both ways -- on this rig X is 21.1 nm forward and 14.7 nm back -- so a
        jog converted with one average number is ~20 % out one way or the other.
        """
        st = self.link.status()
        mean = st.get("um_per_step") or [0.02, 0.02, 0.02]
        table = st.get("um_per_step_fwd" if um >= 0 else "um_per_step_bwd") or mean
        ups = float(table[axis]) or float(mean[axis])
        return int(round(float(um) / ups)) if ups > 0 else 0

    def um_per_step(self, axis: int) -> float:
        """Mean step size, for turning a step count back into um for display."""
        st = self.link.status()
        return float((st.get("um_per_step") or [0.02, 0.02, 0.02])[axis])

    def move_to_steps(self, x_steps: int, y_steps: int) -> tuple:
        """Absolute step targets; returns the targets kim ACCEPTED (after its clamp)."""
        tx = self.link.rpc(cmd="move_to_step", axis="X", position=int(x_steps))["target"]
        ty = self.link.rpc(cmd="move_to_step", axis="Y", position=int(y_steps))["target"]
        return (int(tx), int(ty))

    def zero_counter(self) -> None:
        """Datum: reset kim's X and Y step counters to 0 at the current position."""
        self.link.rpc(cmd="zero_counter", axis="X")
        self.link.rpc(cmd="zero_counter", axis="Y")

    def xy_range(self) -> tuple:
        """((x_min, x_max), (y_min, y_max)) in um -- kim's effective limits."""
        st = self.link.status()
        return (_axis_range_um(st, 0), _axis_range_um(st, 1))

    def move_image_px(self, dx: float, dy: float, context: dict | None = None) -> list:
        """Shift the IMAGE by (dx, dy) px using kim's camera calibration.

        This is how the camera moves the sample on the KIM rig: kim holds the
        measured px/step table (per voltage and direction, including the 90 deg
        mounting and the crosstalk), so the camera never has to know how the
        stage is oriented. ``context`` is our image geometry; kim refuses the
        move if the table was measured under a different one. Returns the
        signed [x, y] steps kim issued. Raises if kim is not calibrated.
        """
        return self.link.rpc(cmd="move_image_px", dx=float(dx), dy=float(dy),
                             context=context)["steps"]


class KimZFocus:
    """ZFocus over kim axis Z, in MICROMETRES."""

    owns_limits = True
    open_loop = True      # slip-stick: approach from one direction, wait to arrive

    def __init__(self, link: KimLink, settle_timeout_s: float = 20.0,
                 poll_s: float = 0.03):
        self.link = link
        self.settle_timeout_s = settle_timeout_s
        self.poll_s = poll_s
        self._target_steps: int | None = None

    def open(self) -> None:
        self.link.open()

    def close(self) -> None:
        self.link.close()

    def available(self) -> tuple:
        return self.link.available()

    def reconnect(self) -> tuple:
        return self.link.reconnect()

    def z_unit(self) -> str:
        return "um"

    def read_z(self) -> float:
        return float(self.link.status()["position_um"][2])

    def set_z(self, um: float) -> None:
        reply = self.link.rpc(cmd="move_to_um", axis="Z", position=float(um))
        # kim replies with the step target it ACCEPTED (after its own clamp), so
        # waiting for exactly that count can never hang on a clamped target.
        self._target_steps = int(reply["target"])

    def z_range(self) -> tuple:
        return _axis_range_um(self.link.status(), 2)

    def wait_settled(self, tick=None) -> None:
        """Block until Z has reached the last set_z target and stopped.

        Uses REQUESTS, not the 8 Hz cache: right after a move command the cache
        can still describe the previous target (suite gotcha #2), and an
        autofocus level read on a stale "not moving" would score the wrong Z.
        ``tick()``, if given, is called on every poll -- the autofocus uses it to
        keep showing live frames while Z walks.
        """
        if self._target_steps is None:
            return
        t_end = time.monotonic() + self.settle_timeout_s
        while True:
            st = self.link.fresh_status()
            if (not st["moving"][2]
                    and int(st["position_steps"][2]) == self._target_steps):
                return
            if time.monotonic() > t_end:
                raise TimeoutError(
                    f"kim Z did not reach {self._target_steps} steps "
                    f"(at {st['position_steps'][2]}) in {self.settle_timeout_s:.0f} s")
            if tick is not None:
                tick()                 # a camera grab paces the loop by itself
            else:
                time.sleep(self.poll_s)
