"""The wire protocol -- ports, topics, and (de)serialisation (section 6).

This ONE file is shared by the service and the client so their message shapes
can never drift. No sockets, no threads: pure data mapping.

Transport (fixed across every module in the suite):
  * Commands  : REQ/REP, JSON in -> JSON out. Reply is always
                {"ok": true, ...} or {"ok": false, "error": "..."}.
                Fire-and-forget: ok=true means *accepted*, not *arrived*.
  * Telemetry : PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Ports are declared in module.toml (5605 / 5606) and mirrored here as defaults.
"""

from __future__ import annotations

import math
from dataclasses import asdict, fields

from ..config import Config, _cast
from ..rotator import RotatorStatus

# -- addressing ------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5605  # REP (commands)
DEFAULT_PUB_PORT = 5606  # PUB (status + events)

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def _json_safe(v):
    """NaN / inf are not JSON: they travel as null (Python would write NaN)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


# -- status --------------------------------------------------------------- #
def status_to_dict(s: RotatorStatus) -> dict:
    """RotatorStatus -> plain dict for the PUB frame / a `status` reply."""
    return {k: _json_safe(v) for k, v in asdict(s).items()}


# -- config --------------------------------------------------------------- #
def _groups(cfg: Config) -> dict:
    """Every config group by its wire name. A NEW GROUP goes here too (gotcha #4)."""
    return {
        "motion": cfg.motion,
        "limits": cfg.limits,
        "frame": cfg.frame,
        "hardware": cfg.hardware,
        "ui": cfg.ui,
    }


def config_to_dict(cfg: Config) -> dict:
    """Nested dict of every config group (for `get_config`)."""
    return {name: asdict(obj) for name, obj in _groups(cfg).items()}


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Write ``data`` INTO an existing Config in place.

    In place so shared references (the brain holds the same object) stay
    valid. Unknown groups/keys are ignored; every value is cast to the
    field's type, so a JSON "false" string cannot become True (gotcha #3).
    """
    groups = _groups(cfg)
    for group_name, values in (data or {}).items():
        obj = groups.get(group_name)
        if obj is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(obj)}
        for key, val in values.items():
            if key in types and val is not None:
                setattr(obj, key, _cast(val, types[key]))
