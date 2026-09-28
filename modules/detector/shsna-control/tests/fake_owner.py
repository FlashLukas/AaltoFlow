"""A FAKE signalhound service: the owner's TG contract, nothing else.

It speaks the contract the real owner promises (tg_sweep_acquire,
get_tg_trace, tg_abort, the tg_* status keys) over real ZeroMQ sockets, so the
remote backend is tested exactly as it will run -- but with knobs a test can
turn: no TG, a sweep that fails, an owner that is slow to ADOPT a new sweep in
its status (the stale-frame trap, gotcha #17), a grid that is NOT the one
asked for (the analyser chooses its bins), and an owner that simply stops.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import zmq


class FakeOwner:
    def __init__(self, cmd_port: int, pub_port: int, *, points: int = 101,
                 sweep_s: float = 0.15, tg_attached: bool = True, status_hz: float = 20.0):
        self.cmd_port, self.pub_port = cmd_port, pub_port
        self.points = points
        self.sweep_s = sweep_s
        self.status_hz = status_hz
        self.adopt_delay_s = 0.0         # status keeps showing the OLD ids this long
        self.fail_next = ""              # the next sweep fails with this tg_error
        self.hw_error = ""
        self.requests: list[dict] = []   # every command received, in order
        self._lock = threading.Lock()
        self.supports_grid = True      # False = an owner older than tg_grid
        #: added to the first bin of every sweep: an analyser whose grid does
        #: NOT land where it was asked (a windowed sweep must then fall back)
        self.grid_shift_hz = 0.0
        self._st = {"tg_attached": tg_attached, "tg_mode": "parked", "tg_acq_id": 0,
                    "tg_acquiring": False, "tg_sample_id": 0, "tg_error": ""}
        self._shown = dict(self._st)     # what status reports (lags by adopt_delay_s)
        self._adopt_at = 0.0
        self._trace: dict | None = None  # the LAST finished TG acquisition
        self._running: dict | None = None
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ------------------------------------------------------------
    def start(self) -> "FakeOwner":
        self._rep = self._ctx.socket(zmq.REP)
        self._rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        for fn in (self._serve, self._publish, self._worker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)

    @property
    def tg_attached(self) -> bool:
        return self._st["tg_attached"]

    @tg_attached.setter
    def tg_attached(self, v: bool) -> None:
        with self._lock:
            self._st["tg_attached"] = bool(v)

    # ---- the status the owner shows ------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            if time.monotonic() >= self._adopt_at:
                self._shown = dict(self._st)
            st = dict(self._shown)
            st["tg_attached"] = self._st["tg_attached"]
        st["hw_error"] = self.hw_error
        return st

    # ---- threads ----------------------------------------------------------------------
    def _publish(self) -> None:
        while not self._stop.is_set():
            self._pub.send_multipart([b"status", json.dumps(self.status()).encode("utf-8")])
            time.sleep(1.0 / self.status_hz)
        self._pub.close(0)

    def _serve(self) -> None:
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                msg = self._rep.recv_json()
                self.requests.append(msg)
                try:
                    reply = self._dispatch(msg)
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                self._rep.send_json(reply)
        self._rep.close(0)

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "tg_sweep_acquire":
            with self._lock:
                if not self._st["tg_attached"]:
                    return {"ok": False, "error": "no tracking generator attached"}
                if self._running is not None:
                    return {"ok": False, "error": "a TG sweep is already running"}
                lo, hi = float(msg["start_hz"]), float(msg["stop_hz"])
                if not (10.0 <= lo < hi <= 4.4e9):
                    return {"ok": False, "error": f"bad range {lo:g}-{hi:g} Hz"}
                n = self._st["tg_acq_id"] + 1
                self._st.update(tg_acq_id=n, tg_acquiring=True, tg_mode="sweep", tg_error="")
                self._running = {"id": n, "t_done": time.monotonic() + self.sweep_s,
                                 "req": dict(msg), "fail": self.fail_next}
                self.fail_next = ""
                self._adopt_at = time.monotonic() + self.adopt_delay_s
            return {"ok": True, "tg_acq_id": n}
        if cmd == "get_tg_trace":
            with self._lock:
                t = self._trace
                want = msg.get("id")
                if t is None or (want is not None and int(want) != t["id"]):
                    return {"ok": False, "error": f"TG acquisition #{want} is not the last finished one"}
                return {"ok": True, **t}
        if cmd == "tg_grid" and self.supports_grid:
            # the owner's grid query: the bins a sweep of this band WOULD use,
            # without sweeping (the same rule as _make_trace)
            g = self._grid({"start_hz": msg["start_hz"], "stop_hz": msg["stop_hz"],
                            "points": msg.get("points", self.points)})
            return {"ok": True, "start_hz": g[0], "bin_hz": g[1], "points": g[2],
                    "predicted": False}
        if cmd == "tg_abort":
            with self._lock:
                r = self._running
                if r is not None:
                    self._running = None
                    self._st.update(tg_acquiring=False, tg_mode="parked",
                                    tg_error=f"TG acquisition #{r['id']} aborted")
            return {"ok": True}
        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                r = self._running
                if r is not None and time.monotonic() >= r["t_done"]:
                    # busy cleared and the result stored in ONE critical
                    # section, as the real owner promises (gotcha #28)
                    self._running = None
                    if r["fail"]:
                        self._st.update(tg_acquiring=False, tg_mode="parked", tg_error=r["fail"])
                    else:
                        self._trace = self._make_trace(r)
                        self._st.update(tg_acquiring=False, tg_mode="parked",
                                        tg_sample_id=r["id"], tg_error="")
            time.sleep(0.005)

    def _grid(self, req: dict) -> tuple:
        n = min(1001, int(req.get("points", self.points)))
        start = float(np.ceil(req["start_hz"] / 1e3) * 1e3) + float(self.grid_shift_hz)
        return start, (float(req["stop_hz"]) - start) / (n - 1), n

    def _make_trace(self, r: dict) -> dict:
        req = r["req"]
        # The ANALYSER's grid: starts on a whole kHz at or above the request
        # and spreads the points over the band, at most 1001 (the SA API
        # clamps silently) -- deliberately not the request's own numbers, so a
        # test sees which grid the client files.
        start, bin_hz, n = self._grid(req)
        f = start + bin_hz * np.arange(n)
        # dB relative to the TG output, as the real TG44A reports it: the
        # bench's 20 dB pad and a little cable loss (level_dbm is ignored)
        db = -19.4 - 0.2 * np.sqrt(f / 1e9)
        return {"id": r["id"], "start_hz": start, "bin_hz": bin_hz, "points": n,
                "db": db.tolist(), "unit": "dB", "level_dbm": None, "overload": False}
