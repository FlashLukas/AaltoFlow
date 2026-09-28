"""A FAKE signalhound service: just the tracking-generator part of its contract.

The real owner opens the USB analyser + TG; this one keeps a dict. It speaks the
same wire contract (REQ/REP JSON commands, PUB/SUB status frames) on whatever
ports a test gives it, so shsg's real backend (backends/remote_sa.py) can be
tested offline, including the awkward cases:

  * `apply_delay_s`: the owner ACCEPTS tg_cw at once but the new value appears
    in its published status only later -- the case where a careless client
    would show (and a scan would accept) a value that is not applied yet;
  * tg_mode "sweep": a network-analyser sweep holds the TG -> tg_cw refused
    with "busy: TG sweep running";
  * tg_attached False -> tg_cw refused;
  * tg_mode "unknown": the TG state cannot be read; a tg_cw makes it known;
  * "off" is a PARK (the TG44A cannot be silenced): tg_cw {on: false} puts
    it in tg_mode "parked" at tg_park_hz (10 kHz) and the minimum level;
  * `hw_error`: the owner's own hardware read failed.

Contract (as agreed with the signalhound module, 2026-09-28):
  tg_cw {on?, freq_hz?, level_dbm?} -> {"ok": true, "tg_cw": {on, freq_hz, level_dbm}}
  status keys tg_attached, tg_mode ("unknown"|"parked"|"cw"|"sweep"), tg_cw_on,
  tg_cw_freq_hz, tg_cw_level_dbm, tg_park_hz, hw_error
"""

from __future__ import annotations

import json
import threading
import time

import zmq

FREQ_RANGE = (10.0, 4.4e9)
LEVEL_RANGE = (-30.0, -10.0)


class FakeOwner:
    def __init__(self, cmd_port: int, pub_port: int, *, on: bool = False,
                 freq_hz: float = 1e9, level_dbm: float = -20.0,
                 attached: bool = True, mode: str | None = None,
                 apply_delay_s: float = 0.0, status_hz: float = 20.0):
        self.cmd_port, self.pub_port = cmd_port, pub_port
        self.apply_delay_s = apply_delay_s
        self.status_dt = 1.0 / status_hz
        self.lock = threading.Lock()
        self.state = {
            "tg_attached": attached,
            "tg_mode": mode or ("cw" if on else "parked"),
            "tg_cw_on": on,
            "tg_cw_freq_hz": freq_hz,
            "tg_cw_level_dbm": level_dbm,
            "tg_park_hz": 10_000.0,
            "hw_error": "",
            "sweeping": False,          # an SNA key shsg must ignore
        }
        self.requests: list[dict] = []      # every command received, in order
        self.applied_at: list[tuple[float, dict]] = []   # (monotonic, applied tg_cw)
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeOwner":
        self._rep = self._ctx.socket(zmq.REP)
        self._pub = self._ctx.socket(zmq.PUB)
        self._rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        self._pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        self._t_rep = threading.Thread(target=self._rep_loop, daemon=True)
        self._t_pub = threading.Thread(target=self._pub_loop, daemon=True)
        self._t_rep.start()
        self._t_pub.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._t_rep.join(timeout=2)
        self._t_pub.join(timeout=2)

    def set(self, **kw) -> None:
        """Change the owner's state from the test (e.g. mode='sweep')."""
        with self.lock:
            self.state.update(kw)

    def tg_cw_requests(self) -> list[dict]:
        return [r for r in self.requests if r.get("cmd") == "tg_cw"]

    # ---- threads -------------------------------------------------------------
    def _pub_loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                frame = dict(self.state)
            self._pub.send_multipart([b"status", json.dumps(frame).encode("utf-8")])
            time.sleep(self.status_dt)
        self._pub.close(0)

    def _rep_loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                msg = self._rep.recv_json()
                self.requests.append(msg)
                self._rep.send_json(self._handle(msg))
        self._rep.close(0)

    def _handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "status":
            with self.lock:
                return {"ok": True, "status": dict(self.state)}
        if cmd != "tg_cw":
            return {"ok": False, "error": f"unknown command: {cmd!r}"}
        with self.lock:
            st = dict(self.state)
        if not st["tg_attached"]:
            return {"ok": False, "error": "no TG attached"}
        if st["tg_mode"] == "sweep":
            return {"ok": False, "error": "busy: TG sweep running"}
        new = {"on": st["tg_cw_on"], "freq_hz": st["tg_cw_freq_hz"],
               "level_dbm": st["tg_cw_level_dbm"]}
        for key in ("on", "freq_hz", "level_dbm"):
            if key in msg and msg[key] is not None:
                new[key] = msg[key]
        if not FREQ_RANGE[0] <= float(new["freq_hz"]) <= FREQ_RANGE[1]:
            return {"ok": False, "error": "frequency out of range"}
        if not LEVEL_RANGE[0] <= float(new["level_dbm"]) <= LEVEL_RANGE[1]:
            return {"ok": False, "error": "level out of range"}
        if self.apply_delay_s > 0:
            threading.Timer(self.apply_delay_s, self._apply, args=(new,)).start()
        else:
            self._apply(new)
        return {"ok": True, "tg_cw": new}

    def _apply(self, new: dict) -> None:
        with self.lock:
            self.state.update(tg_cw_on=bool(new["on"]),
                              tg_cw_freq_hz=float(new["freq_hz"]),
                              tg_cw_level_dbm=float(new["level_dbm"]),
                              tg_mode="cw" if new["on"] else "parked")
            self.applied_at.append((time.monotonic(), dict(new)))
