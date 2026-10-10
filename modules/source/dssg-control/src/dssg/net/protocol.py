"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite's standard design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
Ports are this module's own (module.toml), so every service can share one PC.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5591        # must match module.toml [ports]
DEFAULT_PUB_PORT = 5592

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Synthesizer Status dataclass -> plain dict for the wire (every field).
    The sweeps' keys (ramping, frequency_ramp_id, ...) go to the TOP level:
    a ramp block's `done` names them as plain status keys."""
    d = {f.name: getattr(status, f.name) for f in fields(status) if f.name != "sweep"}
    d.update(getattr(status, "sweep", None) or {})
    return d


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'signal': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Unknown groups/keys are ignored. A value that
    arrives as TEXT for a number/bool field (a hand-written client) goes through
    the same parser as the .ini, so "False" does not become True (gotcha #3)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k not in types:
                continue
            if isinstance(v, str) and types[k] in ("bool", "int", "float"):
                v = _cast(v, types[k])
            setattr(grp, k, v)
