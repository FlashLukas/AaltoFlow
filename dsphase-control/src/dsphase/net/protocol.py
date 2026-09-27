"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite's standard design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5589        # declared in module.toml; overridable per PC
DEFAULT_PUB_PORT = 5590

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """PhaseShifter Status dataclass -> plain dict for the wire (every field)."""
    return asdict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'signal': {...}, 'device': {...}}.
    asdict walks every group, so a new group travels without an edit here."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the simulator holds cfg.device) stay valid. Unknown groups
    and keys are ignored rather than invented."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k in types:
                setattr(grp, k, _coerce(v, types[k]))


def _coerce(value, type_name):
    """Give a value from the wire the field's declared type. JSON from a script
    or the console may carry "5.625" for a float or "off" for a bool; stored as
    is, a string step size would crash the rounding and bool("off") is True
    (gotcha #3). The .ini reader's _cast already knows these rules."""
    if type_name in ("bool", bool) and isinstance(value, bool):
        return value
    if type_name in ("float", float) and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return float(value)
    return _cast(str(value), type_name)
