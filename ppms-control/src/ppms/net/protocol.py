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

DEFAULT_CMD_PORT = 5579
DEFAULT_PUB_PORT = 5580

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def _num(v):
    """JSON has no NaN; send None (null) for "no reading yet"."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def status_to_dict(status) -> dict:
    """Cryostat Status dataclass -> plain dict for the wire.

    `measured_field_mT` is deliberately the same key clMag uses, so anything
    that already reads a magnet's field off a status stream (vna-control's
    field source) reads this one with the same code."""
    return {k: _num(v) for k, v in asdict(status).items()}


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'field': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if hasattr(grp, k):
                setattr(grp, k, v)
