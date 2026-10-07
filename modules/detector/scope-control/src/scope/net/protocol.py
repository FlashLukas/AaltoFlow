"""The wire protocol shared by service and client.

Two ZeroMQ sockets, same design as every module in the suite:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

A TRACE never rides in the status stream (2 x 1000 numbers ten times a second
for nothing). It is fetched on request with `get_trace`, as JSON lists of
floats (null for NaN). The status carries the NUMBERS (per-channel values,
phase) of the live average and of the latched sample.
"""

from __future__ import annotations

import math
from dataclasses import asdict, fields, is_dataclass

import numpy as np

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5633
DEFAULT_PUB_PORT = 5634

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"

def status_to_dict(status: dict) -> dict:
    """The Scope's status dict -> plain JSON-safe dict for the wire."""
    return json_safe(dict(status))


def json_safe(v):
    """NaN/inf -> None. Python's json writes the token `NaN`, which is not
    valid JSON: strict parsers (and many non-Python clients) reject the whole
    message. None travels as `null` and means "no value"."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (np.floating, np.integer, np.bool_)):
        return json_safe(v.item())
    if isinstance(v, np.ndarray):
        return json_safe(v.tolist())
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


def decode_array(values) -> np.ndarray:
    """[..., null, ...] -> float array (null -> nan)."""
    return np.array([math.nan if v is None else v for v in values], dtype=float)


def trace_to_wire(trace: dict) -> dict:
    """A trace dict from the Scope -> JSON-safe (arrays become lists)."""
    return json_safe(trace)


def trace_from_wire(d: dict) -> dict:
    """The inverse: every list of numbers back to a numpy array, NaN for nulls."""
    out = {}
    for k, v in d.items():
        if k == "ok":
            continue
        if isinstance(v, list) and (not v or isinstance(v[0], (int, float, type(None)))):
            out[k] = decode_array(v)
        else:
            out[k] = v
    return out


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'scan': {...}, 'sim': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the simulator holds the same `sim` group) stay valid.
    Text from a hand-typed client goes through the .ini parser (gotcha #3)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not is_dataclass(grp) or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k in types:
                setattr(grp, k, _cast(v, types[k]))
