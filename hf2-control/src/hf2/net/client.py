"""The client: talk to an Hf2Service, present a LockIn-compatible facade.

A GUI or a script can hold an Hf2Client exactly where it would hold a LockIn:
same setters, same status() attribute names, same get_config()/apply_config(),
same `_on_event` hook. Only the address changes.

It adds one thing a local LockIn does not need: `acquire_blocking()`, the
trigger-then-wait-for-MY-id loop, so a plain script gets a settled sample in
one line without re-implementing the stale-status guard.
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


def _nan_list(v, n=2):
    """JSON null (a reading that does not exist yet) back to NaN."""
    if not isinstance(v, list):
        return [float("nan")] * n
    return [float("nan") if x is None else x for x in v]


class RemoteStatus:
    """Same attributes a caller reads off the LockIn's Status."""

    def __init__(self, d: dict):
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.reference = d.get("reference", ["internal", "internal"])
        self.freq_set_Hz = _nan_list(d.get("freq_set_Hz"))
        self.ref_freq_Hz = _nan_list(d.get("ref_freq_Hz"))
        self.tc_set_s = _nan_list(d.get("tc_set_s"))
        self.tc_s = _nan_list(d.get("tc_s"))
        self.order = d.get("order", [1, 1])
        self.pll_locked = d.get("pll_locked", [None, None])
        self.settle_s = _nan_list(d.get("settle_s"))
        live = d.get("live") or {}
        self.live = {k: _nan_list(live.get(k)) for k in
                     ("x", "y", "r", "theta_deg", "freq_Hz", "aux_in")}
        self.acq_id = d.get("acq_id", 0)
        self.acquiring = d.get("acquiring", False)
        self.acq_progress = d.get("acq_progress", 0.0)
        sample = d.get("sample") or {}
        self.sample = {k: (_nan_list(v) if isinstance(v, list) else v)
                       for k, v in sample.items()}
        self.describe_rev = d.get("describe_rev")


class Hf2Client:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
        self._timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._req = self._new_req(f"tcp://{host}:{cmd_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync via get_config / apply_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- LockIn-compatible surface --------------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        self.get_config()
        return info

    def get_config(self) -> Config:
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        return RemoteStatus(self._status_dict())

    def set_time_constant(self, channel: int, tc_s: float):
        return self._cmd({"cmd": "set_time_constant", "channel": int(channel),
                          "time_constant_s": float(tc_s)})

    def set_order(self, channel: int, order: int):
        return self._cmd({"cmd": "set_order", "channel": int(channel), "order": int(order)})

    def set_frequency(self, channel: int, hz: float):
        return self._cmd({"cmd": "set_frequency", "channel": int(channel),
                          "frequency_Hz": float(hz)})

    def set_reference(self, channel: int, mode: str):
        return self._cmd({"cmd": "set_reference", "channel": int(channel), "mode": str(mode)})

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        r = self._cmd({"cmd": "acquire"})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "acquire refused"))
        return int(r["acq_id"])

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its sample.

        The wait checks the id before the flag. Right after the trigger, the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring" -- trusting that flag alone would return the previous
        point's sample.
        """
        n = self.acquire()
        limit = timeout_s if timeout_s is not None else self.cfg.acquisition.timeout_s
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("acq_id") == n and not st.get("acquiring", True):
                sample = st.get("sample") or {}
                if sample.get("acq_id") == n:
                    return sample
            time.sleep(poll_s)
        raise TimeoutError(f"acquisition {n} did not finish within {limit:g} s")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self, endpoint: str):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(endpoint)
        return s

    def _cmd(self, d: dict) -> dict:
        with self._req_lock:
            self._req.send_json(d)
            try:
                return self._req.recv_json()
            except zmq.Again:
                # a timed-out REQ socket is stuck; rebuild it
                endpoint = self._req.LAST_ENDPOINT
                self._req.close(0)
                self._req = self._new_req(endpoint.decode() if isinstance(endpoint, bytes)
                                          else endpoint)
                return {"ok": False, "error": "service did not respond (timeout)"}

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                try:
                    topic, payload = self._sub.recv_multipart()
                except zmq.ZMQError:
                    break
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
