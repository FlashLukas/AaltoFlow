"""The wire protocol shared by service and client.

Two ZeroMQ sockets, same design as every module in the suite:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

A SPECTRUM never rides in the status stream (3648 numbers ten times a second
would be ~70 kB/s of mostly repeated data). It is fetched on request with
`get_trace`, as a plain JSON list of floats (null for NaN). The wavelength grid
is not sent with every spectrum either: it is fixed by the instrument's
calibration and fetched once with `get_wavelengths`.
"""

from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np

from ..config import Config

DEFAULT_CMD_PORT = 5603
DEFAULT_PUB_PORT = 5604

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"

#: the array keys a trace dict can carry
TRACE_ARRAYS = ("spectrum", "wavelengths_nm")


def status_to_dict(status) -> dict:
    """Spectrometer Status dataclass -> plain JSON-safe dict for the wire."""
    return json_safe(asdict(status))


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
    """A trace dict from the Spectrometer -> JSON-safe. The wavelength grid is
    left out (get_wavelengths serves it once)."""
    return json_safe({k: v for k, v in trace.items() if k != "wavelengths_nm"})


def trace_from_wire(d: dict, wavelengths_nm: np.ndarray | None = None) -> dict:
    """The inverse: the array back to numpy, NaN for nulls."""
    out = {k: (math.nan if v is None else v) for k, v in d.items() if k != "spectrum"}
    out.pop("ok", None)
    if "spectrum" in d:
        out["spectrum"] = decode_array(d["spectrum"])
    if wavelengths_nm is not None:
        out["wavelengths_nm"] = np.asarray(wavelengths_nm, dtype=float)
    return out


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'scan': {...}, 'sim': {...}}."""
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
