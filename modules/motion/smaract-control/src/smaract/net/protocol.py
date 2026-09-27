"""The wire protocol -- ports, topics, and (de)serialisation (section 6).

This ONE file is shared by the service and the client so their message shapes
can never drift. It contains no sockets and no threads: pure data mapping.

Transport (fixed across every module in the suite):
  * Commands  : REQ/REP, JSON in -> JSON out. Reply is always
                {"ok": true, ...} or {"ok": false, "error": "..."}.
                Fire-and-forget: ok=true means *accepted*, not *arrived*.
  * Telemetry : PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Ports: declared in module.toml (5597 / 5598); these defaults mirror it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, fields

from ..config import GROUPS, Config, _cast
from ..smaract import SmaractStatus

# -- addressing ------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5597  # REP (commands)
DEFAULT_PUB_PORT = 5598  # PUB (status + events)

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


# -- status --------------------------------------------------------------- #
def status_to_dict(s: SmaractStatus) -> dict:
    """SmaractStatus -> plain dict for the PUB frame / a `status` reply.

    NaN (no reading yet) travels as null: Python's json would write the bare
    token NaN, which is not JSON and which other tools refuse.
    """
    d = asdict(s)
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            d[k] = None
    return d


# -- config --------------------------------------------------------------- #
def config_to_dict(cfg: Config) -> dict:
    """Nested dict of every config group (for `get_config`)."""
    return {g: asdict(getattr(cfg, g)) for g in GROUPS}


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Write ``data`` INTO an existing Config in place.

    In place so any shared references (the brain holds the same object) stay
    valid. Unknown groups/keys are ignored. Each value goes through the INI
    caster, so a "False" string from a hand-typed request is False, not True
    (gotcha #3).
    """
    for group_name, values in (data or {}).items():
        if group_name not in GROUPS or not isinstance(values, dict):
            continue
        obj = getattr(cfg, group_name)
        types = {f.name: f.type for f in fields(obj)}
        for key, val in values.items():
            if key in types:
                setattr(obj, key, _cast(val, types[key]))
