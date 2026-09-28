"""The wire protocol -- ports, topics, and (de)serialisation (§6 of the guide).

This ONE file is shared by the service and the client so their message shapes
can never drift.  It contains no sockets and no threads: pure data mapping.

Transport (fixed across every module in the suite):
  * Commands  : REQ/REP, JSON in -> JSON out.  Reply is always
                {"ok": true, ...} or {"ok": false, "error": "..."}.
                Fire-and-forget: ok=true means *accepted*, not *settled*.
  * Telemetry : PUB/SUB, multipart [topic, json].  Topics: b"status", b"event".

Port allocation (§6 table): instrument n (0-based) uses cmd = 5555 + 2n,
pub = cmd + 1.  The piezo stage is instrument #3 -> 5561 / 5562.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config
from ..piezo import PiezoStatus

# -- addressing ------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5561  # REP (commands)
DEFAULT_PUB_PORT = 5562  # PUB (status + events)

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"

# Accept axis as name or index in commands, e.g. "X", "x", 0, "0".
_AXIS_ALIASES = {
    "x": 0, "X": 0, "0": 0, 0: 0,
    "y": 1, "Y": 1, "1": 1, 1: 1,
}


def parse_axis(value) -> int:
    """Normalise an axis argument to 0/1, or raise ValueError."""
    if value in _AXIS_ALIASES:
        return _AXIS_ALIASES[value]
    raise ValueError(f"bad axis {value!r} (use X/Y or 0/1)")


# -- status --------------------------------------------------------------- #
def status_to_dict(s: PiezoStatus) -> dict:
    """PiezoStatus -> plain dict for the PUB frame / a `status` reply."""
    return asdict(s)


# -- config --------------------------------------------------------------- #
def config_to_dict(cfg: Config) -> dict:
    """Nested dict of every config group (for `get_config`)."""
    return {
        "motion": asdict(cfg.motion),
        "limits": asdict(cfg.limits),
        "relative": asdict(cfg.relative),
        "hardware": asdict(cfg.hardware),
        "ui": asdict(cfg.ui),
    }


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Write ``data`` INTO an existing Config in place.

    In-place so any shared references (the brain holds the same object) stay
    valid.  Unknown keys are ignored.
    """
    groups = {
        "motion": cfg.motion,
        "limits": cfg.limits,
        "relative": cfg.relative,
        "hardware": cfg.hardware,
        "ui": cfg.ui,
    }
    for group_name, values in (data or {}).items():
        obj = groups.get(group_name)
        if obj is None or not isinstance(values, dict):
            continue
        for key, val in values.items():
            if hasattr(obj, key):
                setattr(obj, key, val)
