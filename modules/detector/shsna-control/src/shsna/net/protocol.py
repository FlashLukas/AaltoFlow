"""The wire protocol shared by service and client.

Two ZeroMQ sockets, same design as every module in the suite:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

A TRACE never rides in the status stream (thousands of points ten times a
second would be mostly repeated data). It is fetched on request with
`get_trace`, as a plain list of floats (null = NaN). The frequency grid is not
sent: the analyser's grid is start + bin * i, so start_Hz / bin_Hz / points
reproduce it exactly.
"""

from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np

from ..config import Config

DEFAULT_CMD_PORT = 5627
DEFAULT_PUB_PORT = 5628

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Analyzer Status dataclass -> plain JSON-safe dict for the wire."""
    return json_safe(asdict(status))


def json_safe(v):
    """NaN/inf -> None. Python's json writes the token `NaN`, which is not
    valid JSON: strict parsers (and many non-Python clients) reject the whole
    message. None travels as `null` and means "no value"."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (np.floating, np.integer, np.bool_)):
        return json_safe(v.item())
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


#: the arrays a trace can carry: "raw" (dB rel. TG output), "transmission"
#: (dB against the thru reference), "reference" (the thru itself, dB rel. TG
#: output). A get_trace reply
#: carries the ONE that was asked for.
TRACE_ARRAYS = ("raw", "transmission", "reference")


def trace_to_wire(trace: dict) -> dict:
    """A trace dict from the Analyzer -> JSON-safe (arrays as lists, NaN -> null)."""
    out = {k: v for k, v in trace.items() if k not in TRACE_ARRAYS + ("freqs_Hz", "db")}
    out = json_safe(out)
    for k in TRACE_ARRAYS:
        if k in trace:
            out[k] = json_safe(np.asarray(trace[k], dtype=float).tolist())
    return out


def trace_from_wire(d: dict) -> dict:
    """The inverse: arrays back to numpy (null -> NaN), plus the frequency grid."""
    out = {k: (math.nan if v is None else v) for k, v in d.items() if k not in TRACE_ARRAYS}
    for k in TRACE_ARRAYS:
        if k in d:
            out[k] = np.array([math.nan if v is None else v for v in d[k]], dtype=float)
    out.pop("ok", None)
    out["freqs_Hz"] = out["start_Hz"] + out["bin_Hz"] * np.arange(int(out["points"]))
    return out


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'sweep': {...}, 'sample': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the simulator holds the same Config) stay valid."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if hasattr(grp, k):
                setattr(grp, k, v)
