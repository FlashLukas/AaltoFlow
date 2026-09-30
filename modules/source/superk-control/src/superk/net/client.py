"""The client: talk to a SuperkService, present a brain-compatible facade.

A GUI, a script, or the coordinator can hold a SuperkClient exactly where it
would hold the in-process SuperK brain: same method names (set_emission,
set_power, set_rf, set_filter, set_wavelength, ...), same status() shape, same
get_config()/apply_config(), same `_on_event` hook. The caller does not care
whether the laser is in-process or across the lab -- only the address changes.

One difference that matters: a REFUSED command (emission with an open
interlock) raises in the local brain, and here too -- the service's
{"ok": false, "error": ...} is raised as a ValueError, so the GUI handles both
the same way.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).

LOST-CLIENT GUARD. Every command carries this client's random id (inside its
control identity, control.py). Switching
emission ON sends that id as the "owner", and from then on the same background
thread sends a `ping` every `ping_s` seconds. If this process dies or the
network goes, the pings stop and the service switches emission off after
hardware.client_timeout_s. (A raw client -- scan-core, the console -- sends no
owner: it gets no guard, so a long scan is never cut.)
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config, N_LINES
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the brain's Status."""

    def __init__(self, d: dict):
        z = [0.0] * N_LINES
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.emission_set = d.get("emission_set", False)
        self.emission_on = d.get("emission_on", False)
        self.emission_state = d.get("emission_state", "off")
        self.interlock_ok = d.get("interlock_ok", False)
        self.interlock_code = d.get("interlock_code", 0)
        self.interlock = d.get("interlock", "")
        self.status_bits = d.get("status_bits", 0)
        self.power_set_pct = d.get("power_set_pct", 0.0)
        self.power_pct = d.get("power_pct", 0.0)
        self.inlet_temp_C = d.get("inlet_temp_C", 0.0)
        self.rf_set = d.get("rf_set", False)
        self.rf_on = d.get("rf_on", False)
        self.filter = d.get("filter", "")
        self.filter_min_nm = d.get("filter_min_nm", 0.0)
        self.filter_max_nm = d.get("filter_max_nm", 0.0)
        self.crystal_temp_C = d.get("crystal_temp_C", 0.0)
        self.wavelength_set_nm = d.get("wavelength_set_nm", list(z))
        self.wavelength_nm = d.get("wavelength_nm", list(z))
        self.amplitude_set_pct = d.get("amplitude_set_pct", list(z))
        self.amplitude_pct = d.get("amplitude_pct", list(z))
        self.crystal = d.get("crystal", 0)
        self.emission_guarded = d.get("emission_guarded", False)
        self.hw_error = d.get("hw_error", "")
        self.describe_rev = d.get("describe_rev")


class SuperkClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused``."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000, ping_s: float = 1.0,
                 kind: str = "script",
                 name: str = "superk client"):
        self._control_setup(kind, name)
        self._timeout_ms = timeout_ms
        # a random id per client object: tells the service WHICH client owns
        # the emission, so another client's traffic cannot keep it alive. It
        # is the control identity's id (control.py): every command carries
        # that identity as "client", and the service's lost-client guard
        # reads its "id" -- one id, not two.
        self.client_id = self.identity["id"]
        self._ping_s = float(ping_s)
        self._pinging = False          # True after we switched emission on
        self._ctx = zmq.Context.instance()
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{host}:{cmd_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- brain-compatible surface ------------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        self.start_heartbeat()   # "still here": counted as a viewer / keeps control
        self.get_config()
        return info

    def get_config(self) -> Config:
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        self._must({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        """The parameter manifest. Its limits are LIVE (they follow the active
        crystal), so compare status().describe_rev against its `revision`."""
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def filter_names(self) -> list[str]:
        from ..config import names
        return names(self.cfg.filters.names)

    def wavelength_range(self) -> tuple[float, float]:
        s = self.status()
        return s.filter_min_nm, s.filter_max_nm

    def set_emission(self, on: bool):
        d = {"cmd": "set_emission", "on": bool(on)}
        if on:
            d["owner"] = self.client_id      # arm the service's lost-client guard
        self._must(d)
        self._pinging = bool(on)             # heartbeat while we own emission

    def emission_off(self):
        """Emission OFF -- the safety verb, allowed also while viewing."""
        self._must({"cmd": "emission_off"})
        self._pinging = False

    def reset_interlock(self):
        self._must({"cmd": "reset_interlock"})

    def set_power(self, pct: float):
        self._must({"cmd": "set_power", "power_pct": float(pct)})

    def set_rf(self, on: bool):
        self._must({"cmd": "set_rf", "on": bool(on)})

    def set_filter(self, name: str):
        self._must({"cmd": "set_filter", "filter": str(name)})

    def set_wavelength(self, line: int, nm: float):
        self._must({"cmd": "set_wavelength", "line": int(line), "wavelength_nm": float(nm)})

    def set_amplitude(self, line: int, pct: float):
        self._must({"cmd": "set_amplitude", "line": int(line), "amplitude_pct": float(pct)})

    def set_line(self, line: int, nm: float, pct: float):
        self._must({"cmd": "set_line", "line": int(line), "wavelength_nm": float(nm),
                    "amplitude_pct": float(pct)})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service. If THIS client
        switched emission on (and still owns it), its pings stop here, so the
        service's lost-client guard switches emission off after
        hardware.client_timeout_s."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _must(self, d: dict) -> dict:
        """Send, and raise ValueError on a refusal -- like the local brain does."""
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", "command failed"))
        return r

    def _cmd(self, d: dict) -> dict:
        # say who we are (control.py); the same identity makes every command
        # a heartbeat for the lost-client guard (its "id" is client_id)
        d = self._with_identity(dict(d))
        with self._req_lock:
            self._req.send_json(d)
            try:
                reply = self._req.recv_json()
            except zmq.Again:
                # timed out; the REQ socket is now in a bad state -> rebuild it
                self._reset_req()
                return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the laser took a setting it refused.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

    def _reset_req(self):
        endpoint = self._req.LAST_ENDPOINT
        self._req.close(0)
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        if endpoint:
            self._req.connect(endpoint.decode() if isinstance(endpoint, bytes) else endpoint)

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        last_ping = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if self._pinging and self._ping_s > 0 and now - last_ping >= self._ping_s:
                last_ping = now
                try:
                    self._cmd({"cmd": "ping"})
                except Exception:              # the service may be gone; keep trying
                    pass
            try:
                if poller.poll(200):
                    topic, payload = self._sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == TOPIC_STATUS:
                        with self._lock:
                            self._latest = d
                        self._control_from_status(d)
                    elif topic == TOPIC_EVENT:
                        self._on_event(d.get("level", "info"), d.get("msg", ""))
            except zmq.ZMQError:
                break
