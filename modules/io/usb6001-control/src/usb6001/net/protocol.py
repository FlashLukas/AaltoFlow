"""The wire protocol shared by service and client.

Two ZeroMQ sockets, as in every AaltoFlow module:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends cannot drift apart.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5629
DEFAULT_PUB_PORT = 5630

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Daq Status dataclass -> plain dict for the wire (lists stay lists)."""
    return asdict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts; the per-channel lists become
    lists of dicts: {'ai': {'rate_Hz': ..., 'channels': [{...}, ...]}, ...}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE,
    so every object holding a reference to cfg (brain, GUI) sees them.

    Walks the same shape config_to_dict produced, including the per-channel
    lists (item i of the list goes into channel i). Every value is cast to
    the field's type, so a bool that arrives as "False" is False (gotcha #3).
    Unknown keys are ignored: an older client must not crash a newer service.
    """
    _apply(cfg, d)


def _apply(obj, d) -> None:
    if not isinstance(d, dict):
        return
    for f in fields(obj):
        if f.name not in d:
            continue
        cur, new = getattr(obj, f.name), d[f.name]
        if is_dataclass(cur):
            _apply(cur, new)
        elif isinstance(cur, list):
            if isinstance(new, list):
                for item, vals in zip(cur, new):
                    if is_dataclass(item):
                        _apply(item, vals)
        else:
            try:
                setattr(obj, f.name, _cast(new, f.type))
            except (TypeError, ValueError):
                pass              # a malformed value leaves the old one in place
