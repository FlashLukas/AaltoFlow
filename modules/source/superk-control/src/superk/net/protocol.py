"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite-wide design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
                 A reply means ACCEPTED, not done: poll status for the effect.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5611        # from module.toml
DEFAULT_PUB_PORT = 5612

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Status dataclass -> plain dict for the wire. Per-line values are lists
    of 8 (index 0 = line 1), which is why their settle blocks carry an `index`."""
    return asdict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'startup': {...}, 'limits': {...}}.
    asdict() follows the Config class, so a new group travels automatically."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Each value is cast to the field's type, so a
    JSON 1 for a float field becomes 1.0 and a "false" for a bool becomes False
    (gotcha #3 applies to the wire too)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k not in types:
                continue
            if isinstance(v, bool):
                setattr(grp, k, v if types[k] == "bool" else _cast(str(v), types[k]))
            else:
                setattr(grp, k, _cast(str(v), types[k]))
