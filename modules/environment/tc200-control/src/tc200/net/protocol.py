"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite's contract (docs/DEVELOPER_NOTES.md section 4):
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5613
DEFAULT_PUB_PORT = 5614

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def _num(v):
    """JSON has no NaN; send None (null) for "no reading yet"."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def status_to_dict(status) -> dict:
    """Heater Status dataclass -> plain dict for the wire."""
    return {k: _num(v) for k, v in asdict(status).items()}


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'device': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Values are cast to the field's current type
    (JSON has one number type; an int gain must stay an int)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if not hasattr(grp, k):
                continue
            old = getattr(grp, k)
            if isinstance(old, bool):
                v = v if isinstance(v, bool) else str(v).strip().lower() in (
                    "1", "true", "yes", "on")
            elif isinstance(old, int):
                v = int(round(float(v)))
            elif isinstance(old, float):
                v = float(v)
            setattr(grp, k, v)
