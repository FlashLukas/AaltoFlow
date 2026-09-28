"""The wire protocol shared by service and client.

  * commands  -- REQ/REP, JSON. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics b"status", b"event".

Instrument #7 in the suite's port scheme: cmd = 5555 + 2*7, pub = cmd + 1.
"""

from __future__ import annotations

import math
from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5569
DEFAULT_PUB_PORT = 5570

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """LockIn Status dataclass -> plain dict for the wire.

    NaN is not valid JSON (Python writes the token `NaN`, which strict parsers
    reject), so readings that do not exist yet travel as null.
    """
    return _json_safe(asdict(status))


def _json_safe(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


# ---- config (Settings) over the wire -------------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested dicts: {'ch1': {...}, 'ch2': {...}, ...}."""
    return {name: asdict(getattr(cfg, name)) for name in Config._GROUPS}


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict into an existing Config IN PLACE, so
    shared references stay valid. Unknown groups and keys are ignored."""
    for group, values in d.items():
        if group not in Config._GROUPS or not isinstance(values, dict):
            continue
        grp = getattr(cfg, group)
        for k, v in values.items():
            if hasattr(grp, k):
                setattr(grp, k, v)
