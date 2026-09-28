"""RemoteSa -- TG sweeps through the signalhound SERVICE (the `--real` backend).

WHY a client and not a driver: the USB-TG44A tracking generator can only be
driven through the analyser's API handle, and the SA API lets ONE process hold
that handle. The signalhound service is that process (Lukas's decision,
2026-09-28: signalhound = the only owner of the USB devices; shsng = signal
generator; shsna = this module). So this backend asks the owner for TG sweeps
over ZeroMQ, exactly as camera-control drives kim (remote_kim.py): the RAW
protocol, pyzmq + json, no `signalhound` import, so the two projects stay
decoupled. It claims NO hardware lock -- the owner holds the claim.

The owner's contract (what this file relies on):

  tg_sweep_acquire {start_hz, stop_hz, points?, rbw_hz?, averages?}
        (level_dbm is optional and NOT sent: the TG44A ignores the level in
        sweep mode, measured 2026-09-28)
        -> {"ok": true, "tg_acq_id": n}   refused when no TG, a bad range, or a
           TG sweep already running. The sweep is EXCLUSIVE: the owner pauses its
           spectrum sweeping and any SG CW output, and restores them afterwards.
  get_tg_trace {id?} -> {"ok": true, "id", "start_hz", "bin_hz", "points",
        "db": [...], "unit": "dB", "overload"} of the LAST finished TG
        acquisition (refused if that id is unfinished or aborted). The values
        are dB RELATIVE TO THE TG's calibrated output, not dBm.
  tg_abort
  status keys: tg_attached, tg_mode ("unknown"|"parked"|"cw"|"sweep"), tg_acq_id (last
        started), tg_acquiring, tg_sample_id (last finished), tg_error ("" or
        why the last one failed/aborted), hw_error.

HOW WE WAIT (gotcha #17). Right after tg_sweep_acquire is accepted, the status
frames can still be the ones from BEFORE it -- "not acquiring", the old ids --
and trusting `tg_acquiring` alone would fetch the previous sweep. So we wait on
the ID: done when tg_sample_id == ours. It failed when the owner has ADOPTED
ours (tg_acq_id == ours), is no longer acquiring, and tg_sample_id is still not
ours. The owner clears "busy" and stores the result in one critical section
(gotcha #28), so a frame with our sample id always has our trace behind it.

Read from the PUB stream, not by asking: the brain looks at the owner's health
every 0.1 s, and a status request per look would be pointless traffic. The
owner publishes ~10 Hz; `alive_s` without a frame means it is gone.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import zmq

from .base import SweepFailed


class OwnerLink:
    """One connection to the signalhound service: REQ for commands, SUB for status."""

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5587, pub_port: int = 5588,
                 timeout_ms: int = 2000, alive_s: float = 2.0, retry_s: float = 5.0):
        self.host, self.cmd_port, self.pub_port = host, int(cmd_port), int(pub_port)
        self.timeout_ms = int(timeout_ms)
        self.alive_s = float(alive_s)
        # After a request timed out, fail at once for this long instead of
        # waiting out another timeout (fail fast when the owner is down). A
        # status frame from the owner ends it early: it is talking again.
        self.retry_s = float(retry_s)
        self._down_until = 0.0
        self._ctx = zmq.Context.instance()
        self._req_lock = threading.Lock()
        self._req = None
        self._cache_lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_t = 0.0
        self._stop = threading.Event()
        self._sub_thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.cmd_port}"

    def open(self) -> None:
        with self._req_lock:
            if self._req is None:
                self._make_req()
        if self._sub_thread is None:
            self._stop.clear()
            self._sub_thread = threading.Thread(target=self._sub_loop, name="owner-sub",
                                                daemon=True)
            self._sub_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1.0)
            self._sub_thread = None
        with self._req_lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
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
                    st = json.loads(payload.decode("utf-8"))
                except zmq.Again:
                    continue
                except Exception:
                    continue
                if isinstance(st, dict):
                    self.store(st)
        finally:
            sub.close(0)

    def store(self, st: dict) -> None:
        with self._cache_lock:
            self._cache, self._cache_t = st, time.monotonic()
        self._down_until = 0.0          # the owner is talking: requests may go through

    def cached(self) -> tuple[dict | None, float]:
        """(latest status frame or None, its age in s)."""
        with self._cache_lock:
            st, t = self._cache, self._cache_t
        return (dict(st) if st is not None else None,
                (time.monotonic() - t) if st is not None else float("inf"))

    def rpc(self, **req) -> dict:
        """Send one command. Raises ConnectionError when the owner does not
        answer, SweepFailed on an {"ok": false} reply (its error is the reason)."""
        with self._req_lock:
            if time.monotonic() < self._down_until:
                raise ConnectionError(f"signalhound service at {self.address} is not answering")
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
                raise ConnectionError(f"signalhound service at {self.address} did not answer "
                                      f"'{req.get('cmd')}' within {self.timeout_ms} ms")
        if not isinstance(reply, dict) or not reply.get("ok"):
            err = reply.get("error") if isinstance(reply, dict) else None
            raise SweepFailed(f"signalhound refused {req.get('cmd')}: {err or 'no reason given'}")
        return reply

    def status(self) -> dict:
        """The owner's status: the cached frame while it is fresh, otherwise
        one direct request (the PUB stream may simply not have connected yet)."""
        st, age = self.cached()
        if st is not None and age <= self.alive_s:
            return st
        st = self.rpc(cmd="status").get("status") or {}
        self.store(st)
        return st


class RemoteSa:
    """The SnaBackend that measures through the signalhound service."""

    simulated = False

    def __init__(self, cfg, link: OwnerLink | None = None, clock=time.monotonic):
        hw = cfg.hardware
        self.cfg = cfg
        self.link = link or OwnerLink(hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port,
                                      timeout_ms=hw.timeout_ms, alive_s=hw.alive_s)
        self._clock = clock
        self._id: int | None = None          # the owner's id of OUR running acquisition
        self._t0 = 0.0
        self._asked = (0.0, 0.0, 0)          # the band and point count we asked for
        self._last_grid: dict | None = None  # (start, stop) asked -> grid the owner answered with

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Connect the two sockets. Sends NOTHING to the owner that changes it
        (start writes nothing), and does not raise when the owner is not up
        yet -- start_after in module.toml usually has it running, but a
        restarted owner must not need a restart of this module too."""
        self.link.open()

    def close(self) -> None:
        self.link.close()
        self._id = None

    def idn(self) -> str:
        return f"Signal Hound TG sweeps via the signalhound service at {self.link.address}"

    # ---- health (cached only) ----------------------------------------------
    def health(self) -> str:
        st, age = self.link.cached()
        where = f"signalhound service at {self.link.address}"
        if st is None:
            return f"{where} has not been heard yet (is it running?)"
        if age > self.link.alive_s:
            return f"{where} silent for {age:.0f} s"
        if st.get("hw_error"):
            return f"analyser: {st.get('hw_error')}"
        if not st.get("tg_attached", False):
            return "no tracking generator attached to the analyser"
        return ""

    def owner_status(self) -> dict:
        st, age = self.link.cached()
        st = st or {}
        return {"address": self.link.address,
                "reachable": bool(st) and age <= self.link.alive_s,
                "tg_attached": bool(st.get("tg_attached", False)),
                "tg_mode": str(st.get("tg_mode", "") or ""),
                "hw_error": str(st.get("hw_error", "") or "")}

    # ---- sweeping -----------------------------------------------------------
    def estimate_time_s(self, points, averages) -> float:
        # measured on the lab PC (2026-09-28): 0.2 s + 1.3 ms per point, and the
        # API clamps to 1001 points
        n = min(1001, max(2, int(points)))
        return (0.2 + n * 1.3e-3) * max(1, int(averages))

    def predicted_grid(self, start_Hz, stop_Hz, points):
        """The grid a sweep of this band will use. The ANALYSER chooses its
        bins, so: the grid of a sweep of this very band and point count if we
        made one, else ask the owner (`tg_grid`, 2026-09-28: the grid without
        sweeping -- on the real SA44B a prediction, # VERIFY against a sweep).
        None if neither is possible (owner down, or older than tg_grid): the
        brain then asks for a reference first rather than guess."""
        g = self._last_grid
        if (g and _same(g["req_start"], start_Hz) and _same(g["req_stop"], stop_Hz)
                and g["req_points"] == int(points)):
            return g["start"], g["bin"], g["points"]
        if self.link._sub_thread is None:
            return None                     # never opened: no network (describe at build time)
        try:
            r = self.link.rpc(cmd="tg_grid", start_hz=float(start_Hz),
                              stop_hz=float(stop_Hz), points=int(points))
        except (ConnectionError, SweepFailed):
            return None
        return float(r["start_hz"]), float(r["bin_hz"]), int(r["points"])

    def start_sweep(self, start_Hz, stop_Hz, points, rbw_Hz, averages) -> None:
        req = {"cmd": "tg_sweep_acquire", "start_hz": float(start_Hz),
               "stop_hz": float(stop_Hz), "points": int(points),
               "averages": max(1, int(averages))}
        if rbw_Hz and rbw_Hz > 0:
            req["rbw_hz"] = float(rbw_Hz)
        reply = self.link.rpc(**req)
        if "tg_acq_id" not in reply:
            raise SweepFailed("signalhound accepted the TG sweep but returned no tg_acq_id")
        self._id = int(reply["tg_acq_id"])
        self._t0 = self._clock()
        self._asked = (float(start_Hz), float(stop_Hz), int(points))
        # when the owner should be done: from then on poll() ASKS it rather
        # than wait for its next status frame (lab PC: +0.15-0.2 s per point)
        self._due = self._t0 + self.estimate_time_s(points, averages)
        self._last_ask = 0.0

    def poll(self) -> bool:
        n = self._id
        if n is None:
            raise SweepFailed("no TG sweep running")
        if self._clock() - self._t0 > self.cfg.hardware.sweep_timeout_s:
            self.abort()
            raise SweepFailed(f"TG sweep #{n} did not finish within "
                              f"{self.cfg.hardware.sweep_timeout_s:g} s")
        st = self.link.status()             # ConnectionError when the owner is gone
        now = self._clock()
        if (_int(st.get("tg_sample_id")) != n and now >= getattr(self, "_due", now)
                and now - getattr(self, "_last_ask", 0.0) >= 0.03):
            # overdue by the estimate: one direct request (~1 ms), at most
            # every 30 ms, instead of up to one PUB period of waiting
            self._last_ask = now
            st = self.link.rpc(cmd="status").get("status") or st
            self.link.store(st)
        sample_id = _int(st.get("tg_sample_id"))
        started_id = _int(st.get("tg_acq_id"))
        if sample_id == n:
            return True
        if started_id is not None and started_id > n:
            # a newer TG sweep started, so ours is over -- and it is not the
            # last finished one, so its trace is gone
            raise SweepFailed(f"TG sweep #{n} was superseded by #{started_id} on the analyser")
        if started_id == n and not st.get("tg_acquiring", False):
            why = st.get("tg_error") or "aborted"
            raise SweepFailed(f"TG sweep #{n} failed on the analyser: {why}")
        return False

    def fetch(self) -> dict:
        n = self._id
        if n is None:
            raise SweepFailed("fetch without a finished TG sweep")
        self._id = None
        r = self.link.rpc(cmd="get_tg_trace", id=n)
        if _int(r.get("id")) != n:
            raise SweepFailed(f"asked for TG sweep #{n}, the analyser returned #{r.get('id')}")
        unit = r.get("unit", "dB")
        if unit != "dB":
            # the module's arithmetic is in dB relative to the TG output; a
            # trace in another unit would be subtracted from the wrong thing
            raise SweepFailed(f"TG trace #{n} is in {unit!r}, expected 'dB' (rel. TG output)")
        db = np.array([np.nan if v is None else v for v in r.get("db", [])], dtype=float)
        points = int(r.get("points", db.size))
        if db.size != points or points < 2:
            raise SweepFailed(f"TG trace #{n}: {db.size} values for {points} points")
        grid = {"start": float(r["start_hz"]), "bin": float(r["bin_hz"]), "points": points,
                "req_start": self._asked[0], "req_stop": self._asked[1],
                "req_points": self._asked[2]}
        self._last_grid = grid
        return {"start_Hz": grid["start"], "bin_Hz": grid["bin"], "points": points,
                "rbw_Hz": float(r.get("rbw_hz", 0.0) or 0.0),
                "averages": int(r.get("averages", 0) or 0),
                "db": db, "overload": bool(r.get("overload", False)),
                # the owner's breakdown of this acquisition (queued / configure /
                # sweep / restore / total, s) -- for finding the overhead
                "owner_timing_s": dict(r.get("timing_s") or {})}

    def abort(self) -> None:
        """Abort OUR acquisition only. `tg_abort` takes no id, so we send it
        only while the owner's state does not show that ours is already over
        (finished, or a newer one started) -- the owner refuses a second TG
        sweep while one runs, so a running one that is not over is ours."""
        n, self._id = self._id, None
        if n is None:
            return
        st, _age = self.link.cached()
        st = st or {}
        sample_id, started_id = _int(st.get("tg_sample_id")), _int(st.get("tg_acq_id"))
        if (sample_id is not None and sample_id >= n) or (started_id is not None and started_id > n):
            return
        try:
            self.link.rpc(cmd="tg_abort")
        except Exception:
            pass                             # owner gone: nothing is sweeping for us


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _same(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
