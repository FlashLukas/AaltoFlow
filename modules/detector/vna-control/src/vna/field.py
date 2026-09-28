"""field.py -- where the field the sample sits in is READ from.

A VNA does not know what field its sample is in, but every trace needs it: an
FMR trace without its field is not data. So the analyser LISTENS to a magnet
service's status stream, exactly like any other client of that service: raw
pyzmq, no `mag2d` / `clMag` import, so the projects stay decoupled (suite rule).

This is the brain's job, not the simulator's: the field is latched into every
sample in REAL mode too. In simulation it additionally drives the physics.

It only SUBSCRIBES. It never sends the magnet a command -- a detector that could
move the field would be a surprise nobody wants in the middle of a scan.

Why the MEASURED field and not the setpoint: the sample sees what the Hall probe
sees. While the magnet ramps or overshoots, the resonance moves with it.

Two magnets are understood:
  * mag2d -- the 2-axis vector magnet. It publishes the measured VECTOR
             (measured_bx_mT, measured_by_mT); the VNA turns it into a magnitude
             hypot(Bx, By) and an angle atan2(By, Bx) in degrees. The magnitude
             is therefore never negative: "-50 mT at 0 deg" arrives as
             "50 mT at 180 deg", which is the same field.
  * clMag -- the 1-axis magnet: measured_field_mT, signed, angle 0.
  * ppms  -- the Quantum Design DynaCool (ppms-control): the same key, signed,
             angle 0 (no rotator on the FMR setup).
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass

import zmq

#: the magnets a field can come from (plus "manual"), in the order a GUI lists them
FIELD_SOURCES = ("mag2d", "mag2dcal", "clMag", "ppms", "manual")


@dataclass
class FieldReading:
    field_mT: float
    ok: bool           # False = not the live field (magnet silent / never heard)
    source: str        # human-readable: "manual", "mag2d", "clMag (stale 3.2 s)"
    age_s: float       # seconds since that value was published; nan if never
    angle_deg: float = 0.0   # in-plane field angle; 0 for a 1-axis magnet


class ManualField:
    """A field typed in by hand. The getters are read at every call, so changing
    the config value takes effect at the next sweep."""

    name = "manual"

    def __init__(self, get_mT, get_angle_deg=lambda: 0.0):
        self._get = get_mT
        self._angle = get_angle_deg

    def read(self) -> FieldReading:
        return FieldReading(float(self._get()), True, "manual", 0.0, float(self._angle()))

    def close(self) -> None:
        pass


def _parse_clMag(frame: dict):
    """clMag status frame -> (field_mT, angle_deg), or None if it has no field."""
    v = frame.get("measured_field_mT")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v), 0.0
    return None


def _parse_mag2d(frame: dict):
    """mag2d status frame -> (|B|, angle in degrees) from the measured vector.

    Both components must be present and finite: half a vector is not a field."""
    bx, by = frame.get("measured_bx_mT"), frame.get("measured_by_mT")
    for v in (bx, by):
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
            return None
    return math.hypot(bx, by), math.degrees(math.atan2(by, bx))


# mag2dcal is the SECOND 2-axis magnet module (clMag control philosophy:
# calibration + PI trim + freeze + stabilizer). It publishes the same status
# keys as mag2d on purpose, so it reads with the same parser -- only the
# endpoint differs. Which one is running is the operator's choice.
# ppms-control publishes `measured_field_mT` on purpose, the key clMag uses:
# a 1-axis signed field reads with the same parser.
_PARSERS = {"clMag": _parse_clMag, "mag2d": _parse_mag2d, "mag2dcal": _parse_mag2d,
            "ppms": _parse_clMag}


class RemoteField:
    """Follow a magnet service's measured field from its PUB stream (~10 Hz).

    `kind` = "mag2d" or "clMag" chooses how a status frame is read.

    The socket lives entirely inside one thread (a ZeroMQ socket must not be
    shared between threads); `read()` only copies what that thread stored, so
    it never blocks -- important, because a VNA sweep must not wait on a magnet
    that has been switched off.
    """

    def __init__(self, host: str, pub_port: int, stale_s: float = 2.0,
                 fallback_mT=lambda: 0.0, clock=time.monotonic,
                 kind: str = "clMag", fallback_angle_deg=lambda: 0.0):
        if kind not in _PARSERS:
            raise ValueError(f"unknown magnet kind {kind!r}; one of {tuple(_PARSERS)}")
        self.name = kind
        self._parse = _PARSERS[kind]
        self.endpoint = f"tcp://{host}:{int(pub_port)}"
        self.stale_s = float(stale_s)
        self._fallback = fallback_mT
        self._fallback_angle = fallback_angle_deg
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[float, float] | None = None     # (field, angle)
        self._t: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen, name=f"vna-field-{kind}",
                                        daemon=True)
        self._thread.start()

    def read(self) -> FieldReading:
        with self._lock:
            value, t = self._value, self._t
        if value is None:
            return FieldReading(float(self._fallback()), False,
                                f"manual ({self.name} not heard)", math.nan,
                                float(self._fallback_angle()))
        age = self._clock() - t
        if age > self.stale_s:
            return FieldReading(value[0], False, f"{self.name} (stale {age:.0f} s)", age, value[1])
        return FieldReading(value[0], True, self.name, age, value[1])

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _listen(self) -> None:
        sub = zmq.Context.instance().socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(self.endpoint)          # connects lazily: fine if the magnet is not up yet
        sub.setsockopt(zmq.SUBSCRIBE, b"status")
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if not poller.poll(200):
                    continue
                try:
                    _topic, payload = sub.recv_multipart()
                    frame = json.loads(payload)
                    got = self._parse(frame) if isinstance(frame, dict) else None
                except (ValueError, zmq.ZMQError):
                    continue                 # a malformed frame must not end the thread
                if got is not None:
                    with self._lock:
                        self._value, self._t = got, self._clock()
        finally:
            sub.close(0)


def make_field_source(cfg_field):
    """Build the field source a `config.Field` asks for. Raises on an unknown name.

    The manual value is also the FALLBACK of a remote source before its magnet
    has been heard, so it is read live from the config, not copied."""
    f = cfg_field
    manual = lambda: f.manual_mT                    # noqa: E731 -- read live
    manual_angle = lambda: f.manual_angle_deg       # noqa: E731
    if f.source == "manual":
        return ManualField(manual, manual_angle)
    if f.source == "mag2d":
        return RemoteField(f.mag2d_host, f.mag2d_pub_port, f.stale_s, fallback_mT=manual,
                           kind="mag2d", fallback_angle_deg=manual_angle)
    if f.source == "mag2dcal":
        return RemoteField(f.mag2dcal_host, f.mag2dcal_pub_port, f.stale_s,
                           fallback_mT=manual, kind="mag2dcal",
                           fallback_angle_deg=manual_angle)
    if f.source == "clMag":
        return RemoteField(f.clMag_host, f.clMag_pub_port, f.stale_s, fallback_mT=manual,
                           kind="clMag", fallback_angle_deg=manual_angle)
    if f.source == "ppms":
        return RemoteField(f.ppms_host, f.ppms_pub_port, f.stale_s, fallback_mT=manual,
                           kind="ppms", fallback_angle_deg=manual_angle)
    raise ValueError(f"field source must be one of {FIELD_SOURCES}, got {f.source!r}")
