"""RemoteTG -- the REAL backend: drive the TG44A through the signalhound SERVICE.

Why a client and not a driver: the Signal Hound API reaches the USB-TG44A only
through the spectrum analyser's device handle, and a USB device can be opened by
ONE process. That process is the signalhound service (the owner of both
devices). So this module never touches USB and never imports the vendor
library; it asks the owner, over the suite's ordinary wire contract:

    command  tg_cw {on?, freq_hz?, level_dbm?}   (missing = keep)
             -> {"ok": true, "tg_cw": {...}}  or  {"ok": false, "error": ...}
    status   tg_attached, tg_mode ("unknown"|"parked"|"cw"|"sweep"), tg_cw_on,
             tg_cw_freq_hz, tg_cw_level_dbm (the APPLIED values), tg_park_hz,
             hw_error

THE TG44A CANNOT BE SWITCHED OFF (found on the lab PC, 2026-09-28): it keeps
emitting its last frequency and level even after every program has exited.
So "RF off" is a PARK: the owner moves it to a park frequency (its config,
default 10 kHz) at the minimum level (-30 dBm). tg_cw {on: false} parks.

It speaks the RAW protocol (pyzmq + json, no `signalhound` import), the same way
camera-control talks to kim (camera/backends/remote_kim.py), so the two projects
stay decoupled and can be updated independently.

Design choices, and why:

* Status comes ONLY from the owner's PUB stream (a cache filled by a SUB
  thread), never from what we asked for. So our status can never show a new
  frequency before the owner has applied it (the spirit of gotcha #40): a scan
  waiting for the echo waits for the OWNER's echo.

* read_state() never sends a request. The service publishes our status at 5 Hz
  from that call; a request to a dead owner would block the publisher for the
  whole timeout. The owner publishes at ~10 Hz, so a cache older than ALIVE_S
  means it is gone.

* Commands fail FAST while the owner is known to be down (stale cache): the
  caller gets "signalhound service not reachable" at once instead of waiting
  out a timeout per click.

* No hwlock claim: we own no physical address. The owner claims the USB
  devices; a second shsg is harmless (both would be clients of one owner).

* Encryption (secure.py, README "Encryption and keys"): both sockets speak
  CurveZMQ with the SIGNALHOUND service's key when the lab's policy secures
  signalhound -- the key of the module we talk to, not our own. If the owner
  runs in the other mode than the policy now says (started before it
  changed), a request that gets no answer flips the mode once (no_answer),
  and the status stream follows. One catch specific to this backend: its
  fail-fast rule trusts the status stream, and a stream in the wrong mode is
  silent, so a silent owner is PROBED with one status request (at most every
  PROBE_S, and only on a PC that has a key -- elsewhere the mode cannot be
  wrong and a dead owner still fails at once).

* open() does not fail when the owner is not running yet. The launcher starts
  it first (module.toml start_after), but opening two USB devices takes a few
  seconds, and a module that died because its owner was slow would be worse
  than one that says so in hw_error and picks the TG up as soon as the owner's
  first frame arrives.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import zmq

from .. import secure
from ..control import make_identity


#: the TG44A's minimum level, where the owner parks it (# VERIFY vs its config)
PARK_LEVEL_DBM = -30.0


def _is_owner_down_msg(host: str, port: int) -> str:
    return f"signalhound service not reachable ({host}:{port})"


class RemoteTG:
    """TGSource over the signalhound service. See the module docstring."""

    #: the owner publishes at ~10 Hz; this long without a frame = it is gone
    ALIVE_S = 2.0
    #: a silent owner may be one in the other encryption mode: ask it at most
    #: this often (each probe can cost two timeouts while it is really down)
    PROBE_S = 10.0

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5587,
                 pub_port: int = 5588, timeout_ms: int = 1500,
                 wait_s: float = 5.0):
        self.host = host
        self.cmd_port = int(cmd_port)
        self.pub_port = int(pub_port)
        self.timeout_ms = int(timeout_ms)
        self.wait_s = float(wait_s)
        self._ctx = zmq.Context.instance()
        self._req = None
        self._req_lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_t = 0.0
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()
        self._sub_t: threading.Thread | None = None
        self._probed_at = -1e9               # monotonic time of the last probe
        # Who we are to the signalhound service (control.py): a MACHINE, like
        # the camera driving kim -- a person's analyser GUI holding control
        # must not lock the generator out of the TG (nor its clean shutdown,
        # which parks the TG). Sent with every command.
        self.identity = make_identity("machine", "shsg")

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        """Connect and wait (up to wait_s) for the owner's first status frame.

        Sends NOTHING that changes the TG: adopting its state is just reading
        that first frame. If none arrives we say so and carry on (see the
        module docstring); status carries hw_error until the owner shows up.
        """
        self._make_req()
        self._stop.clear()
        self._sub_t = threading.Thread(target=self._sub_loop, name="shsg-owner-sub",
                                       daemon=True)
        self._sub_t.start()
        end = time.monotonic() + self.wait_s
        while time.monotonic() < end:
            if self._fresh() is not None:
                return
            time.sleep(0.02)
        if self._probe_other_mode():
            return
        # printed text stays ASCII: the launcher reads us through a pipe (gotcha #14)
        print(f"shsg: {_is_owner_down_msg(self.host, self.cmd_port)} -- commands are "
              f"refused until it answers; start the signalhound module", file=sys.stderr)

    def close(self) -> None:
        self._stop.set()
        if self._sub_t is not None:
            self._sub_t.join(timeout=1.0)
            self._sub_t = None
        with self._req_lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    # ---- sockets -----------------------------------------------------------
    def _make_req(self) -> None:
        with self._req_lock:
            if self._req is not None:
                self._req.close(0)
            self._req = self._new_req_socket()

    def _new_req_socket(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # signalhound's key, from the lab keyring, when the policy secures
        # signalhound (secure.py); plain otherwise
        secure.secure_client(s, self.host, "signalhound")
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def _sub_loop(self) -> None:
        # A ZeroMQ socket must stay in the thread that uses it, so the SUB
        # socket is created and closed here.
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            s.setsockopt(zmq.RCVTIMEO, 200)
            s.setsockopt(zmq.LINGER, 0)
            secure.secure_client(s, self.host, "signalhound")     # telemetry too
            s.connect(f"tcp://{self.host}:{self.pub_port}")
            s.setsockopt(zmq.SUBSCRIBE, b"status")
            return s, secure.flip_generation()

        sub, gen = make_sub()
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the owner in the other mode
                    # (secure.no_answer): telemetry follows
                    sub.close(0)
                    sub, gen = make_sub()
                try:
                    _topic, payload = sub.recv_multipart()
                    st = json.loads(payload.decode("utf-8"))
                except zmq.Again:
                    continue
                except Exception:          # a malformed frame must not kill the cache
                    continue
                if isinstance(st, dict):
                    with self._cache_lock:
                        self._cache, self._cache_t = st, time.monotonic()
        finally:
            sub.close(0)

    def _request(self, req: dict) -> dict | None:
        """One request/reply with the owner; None when it did not answer.

        On a timeout the REQ socket is rebuilt, and when the owner may speak
        the other encryption mode (secure.no_answer flipped) the request is
        sent once more in that mode. Safe: a request in the wrong mode never
        reaches the owner, so nothing is done twice."""
        with self._req_lock:
            if self._req is None:
                return None                  # closed
            for attempt in (1, 2):
                try:
                    self._req.send_json(req)
                    return self._req.recv_json()
                except zmq.Again:
                    # A REQ socket that timed out is stuck mid-exchange: rebuild it.
                    self._req.close(0)
                    flipped = secure.no_answer(self.host, "signalhound")
                    self._req = self._new_req_socket()
                    if not (flipped and attempt == 1):
                        return None
        return None

    def _probe_other_mode(self) -> bool:
        """The owner is silent: is it one running in the other encryption mode?

        Only worth asking on a PC that has a key (without one, encrypted is
        impossible, so silence means "down" and we keep failing fast), and at
        most every PROBE_S. A status request that gets an answer -- possibly
        after the mode flipped -- makes the SUB thread reconnect in that mode;
        then we wait briefly for the first frame. True when the owner is back."""
        try:
            secure.own_keys()
        except secure.SecurityError:
            return False
        now = time.monotonic()
        if now - self._probed_at < self.PROBE_S:
            return False
        self._probed_at = now
        if self._request({"cmd": "status", "client": self.identity}) is None:
            return False
        end = time.monotonic() + self.ALIVE_S
        while time.monotonic() < end:
            if self._fresh() is not None:
                return True
            time.sleep(0.02)
        return False

    def _fresh(self) -> dict | None:
        """The owner's latest status frame, or None if it is older than ALIVE_S."""
        with self._cache_lock:
            st, t = self._cache, self._cache_t
        if st is None or time.monotonic() - t > self.ALIVE_S:
            return None
        return st

    # ---- the interface -----------------------------------------------------
    def set_cw(self, on=None, freq_hz=None, level_dbm=None) -> dict:
        if self._fresh() is None and not self._probe_other_mode():
            # fail fast: no frame for ALIVE_S -> the owner is down
            raise ConnectionError(_is_owner_down_msg(self.host, self.cmd_port))
        req = {"cmd": "tg_cw", "client": self.identity}
        if on is not None:
            req["on"] = bool(on)
        if freq_hz is not None:
            req["freq_hz"] = float(freq_hz)
        if level_dbm is not None:
            req["level_dbm"] = float(level_dbm)
        reply = self._request(req)
        if reply is None:
            raise TimeoutError(f"signalhound service did not answer tg_cw within "
                               f"{self.timeout_ms} ms")
        if not reply.get("ok", False):
            # The owner's words, verbatim: "busy: TG sweep running", "no TG
            # attached", "out of range" -- they say what to do.
            raise RuntimeError(f"signalhound refused tg_cw: {reply.get('error', 'failed')}")
        # We deliberately do NOT copy reply["tg_cw"] into our state: a reply
        # means ACCEPTED (the wire contract), and our status must only ever show
        # what the owner PUBLISHES as applied.
        # The reply is RETURNED (2026-10-10) for one flag only: `deferred`
        # True = the owner accepted the value but could not apply it yet (its
        # hardware lock was busy); it applies it a little later. A SWEEP
        # counts those steps (generator.py "the SWEEPS"): for such a step the
        # TG got the value later than the sweep's record says.
        return reply

    def read_state(self) -> dict:
        st = self._fresh()
        with self._cache_lock:
            last = self._cache or {}
        # Values: the owner's echo. With no frame ever, None -- the Generator
        # substitutes its placeholders and hw_error says they are not the TG's.
        mode = last.get("tg_mode")
        values = {
            # "off" is a PARK: never report a parked TG as on
            "rf_on": bool(last.get("tg_cw_on", False)) and mode != "parked",
            "parked": mode == "parked",
            "park_Hz": _num(last.get("tg_park_hz")),
            # the owner parks at the TG's minimum level; it publishes no key
            # for it (yet), so fall back to the TG44A's -30 dBm
            "park_dBm": _num(last.get("tg_park_level_dbm", PARK_LEVEL_DBM)),
            "frequency_Hz": _num(last.get("tg_cw_freq_hz")),
            "power_dBm": _num(last.get("tg_cw_level_dbm")),
            "tg_busy": mode == "sweep",
            # "unknown" (2026-09-28, found on the real kit): the owner cannot
            # read the TG's state -- it may be emitting, left on by another
            # program. Its tg_cw_* values then mean nothing; say so, and let
            # the user set a state explicitly (the owner knows it after that).
            "tg_unknown": mode == "unknown",
        }
        if st is None:
            values.update(reachable=False,
                          hw_error=_is_owner_down_msg(self.host, self.cmd_port))
            return values
        err = ""
        if not st.get("tg_attached", False):
            err = "no tracking generator attached to the signalhound analyser"
        elif st.get("hw_error"):
            err = f"signalhound: {st['hw_error']}"
        values.update(reachable=True, hw_error=err)
        return values

    def idn(self) -> str:
        st = self._fresh()
        if st is None:
            return ""
        return f"USB-TG44A via signalhound at {self.host}:{self.cmd_port}"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
