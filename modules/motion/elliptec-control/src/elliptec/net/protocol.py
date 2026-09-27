"""The wire protocol -- ports, topics, and (de)serialisation (section 6 of the guide).

This ONE file is shared by the service and the client so their message shapes
can never drift.  It contains no sockets and no threads: pure data mapping.

Transport (fixed across every module in the suite):
  * Commands  : REQ/REP, JSON in -> JSON out.  Reply is always
                {"ok": true, ...} or {"ok": false, "error": "..."}.
                Fire-and-forget: ok=true means *accepted*, not *arrived*.
  * Telemetry : PUB/SUB, multipart [topic, json].  Topics: b"status", b"event".

Ports are declared in module.toml (5607 / 5608) and can be overridden per PC
by the launcher; the numbers below are the defaults of the scripts.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from ..config import GROUPS, Config, _cast
from ..mount import MountStatus

# -- addressing ------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5607  # REP (commands)
DEFAULT_PUB_PORT = 5608  # PUB (status + events)

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def parse_axis(value, addresses: list | None = None) -> int:
    """Normalise an axis argument to its index.

    Accepted: an index (0, "1") or, when ``addresses`` is given, a bus address
    written as "@A" (so a console user can say "the mount on address A").
    """
    if isinstance(value, str) and value.startswith("@") and addresses is not None:
        a = value[1:].strip().upper()
        if a in addresses:
            return addresses.index(a)
        raise ValueError(f"no mount on address {a!r} (configured: {','.join(addresses)})")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"bad axis {value!r} (use an index 0..n-1 or @<address>)") from None


# -- status --------------------------------------------------------------- #
def status_to_dict(s: MountStatus) -> dict:
    """MountStatus -> plain dict for the PUB frame / a `status` reply."""
    return asdict(s)


# -- config --------------------------------------------------------------- #
def config_to_dict(cfg: Config) -> dict:
    """Nested dict of every config group (for `get_config`)."""
    return {name: asdict(getattr(cfg, name)) for name in GROUPS}


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Write ``data`` INTO an existing Config in place.

    In place, so shared references (the brain holds the same object) stay
    valid.  Unknown groups/keys are ignored; values go through the INI `_cast`
    so a "False" string from a hand-written client cannot turn into True.
    """
    for group_name, values in (data or {}).items():
        if group_name not in GROUPS or not isinstance(values, dict):
            continue
        obj = getattr(cfg, group_name)
        types = {f.name: f.type for f in fields(obj)}
        for key, val in values.items():
            if key not in types:
                continue
            if types[key] == "bool" and not isinstance(val, str):
                setattr(obj, key, bool(val))       # a real JSON true/false
            else:
                setattr(obj, key, _cast(val, types[key]))
