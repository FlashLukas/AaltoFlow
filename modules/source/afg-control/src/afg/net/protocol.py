"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite's standard shape:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5631
DEFAULT_PUB_PORT = 5632

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status: dict) -> dict:
    """The brain's snapshot is already a flat dict of plain values (keys like
    `a_frequency_Hz`, `b_locked`); copy it so the caller may add fields."""
    return dict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'channel_a': {...}, ...}.
    asdict walks every group, so a new group travels without editing this."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Unknown groups / keys are ignored."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not is_dataclass(grp) or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k not in types:
                continue
            # A hand-written or console-typed config may carry TEXT ("false",
            # "20"). Route text through the same parser the .ini uses, so
            # "false" does not become True (gotcha #3) and "20" becomes 20.0.
            # JSON numbers/bools are coerced too (an int 20 for a float field).
            if types[k] != "str":
                v = _cast(v, types[k])
            setattr(grp, k, v)
