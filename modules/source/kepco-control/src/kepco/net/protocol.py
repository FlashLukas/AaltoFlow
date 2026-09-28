"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite-wide design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
    A reply means ACCEPTED, not done: a new current setpoint is still ramping
    when the reply arrives; poll status (`ramping`) for the effect.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5581
DEFAULT_PUB_PORT = 5582

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def _clean(v):
    """NaN / inf are not valid JSON: send them as null ("not measured yet")."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def status_to_dict(status) -> dict:
    """BipolarSupply Status dataclass -> plain dict for the wire. Every field
    travels, so the client's RemoteStatus can mirror it field for field."""
    return {k: _clean(v) for k, v in asdict(status).items()}


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'output': {...}, 'ramp': {...}}.
    asdict walks every group, so a new group travels without an edit here."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Values are cast to the type of the field they
    replace, so a JSON 1 does not turn a float field into an int (or a "False"
    string into a truthy bool, gotcha #3)."""
    from ..config import _cast
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if not hasattr(grp, k):
                continue
            old = getattr(grp, k)
            if isinstance(old, bool):
                v = v if isinstance(v, bool) else _cast(str(v), "bool")
            elif isinstance(old, int):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
            else:
                v = str(v)
            setattr(grp, k, v)
