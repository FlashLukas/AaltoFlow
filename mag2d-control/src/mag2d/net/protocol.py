"""The wire protocol shared by service and client.

Two ZeroMQ sockets:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5575
DEFAULT_PUB_PORT = 5576

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"

#: The status keys of the suite contract, in one list, so a test can check the
#: service publishes every one of them (vna-control and scan-core rely on them).
CONTRACT_STATUS_KEYS = [
    "state", "energized",
    "setpoint_field_mT", "setpoint_angle_deg", "setpoint_bx_mT", "setpoint_by_mT",
    "measured_bx_mT", "measured_by_mT", "measured_field_mT",
    "measured_magnitude_mT", "measured_angle_deg", "error_mT",
    "field_stable", "output_V", "hall_V", "temp_C",
    "water_ok", "water_bypass", "temp_monitor", "fault", "describe_rev",
]


def json_safe(v):
    """NaN/inf -> None. Python's json writes the token `NaN`, which is not valid
    JSON: strict parsers reject the whole message. None travels as `null`."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


def status_to_dict(status) -> dict:
    """Controller Status dataclass -> plain dict for the wire (NaN -> null)."""
    return json_safe(asdict(status))


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'control': {...}, ...}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the simulator holds cfg.sim, cfg.hall) stay valid.

    Values are cast to the type of the field they replace: JSON has one number
    type, and a bool that arrives as 0/1 must still be a bool here.
    """
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if not hasattr(grp, k):
                continue
            old = getattr(grp, k)
            if isinstance(old, bool):
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
            setattr(grp, k, v)
