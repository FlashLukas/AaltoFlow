"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite-wide design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5601
DEFAULT_PUB_PORT = 5602

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Monochromator Status dataclass -> plain dict for the wire.

    asdict keeps every field, so a new status field travels without anyone
    remembering to add it here."""
    return asdict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'gratings': {...}, ...}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Values are cast to the type of the field they
    replace: JSON has no int/float distinction and a bool sent as the text
    "false" must not become True (gotcha #3)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if not hasattr(grp, k):
                continue
            old = getattr(grp, k)
            if isinstance(old, bool):
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                v = int(float(v))
            elif isinstance(old, float):
                v = float(v)
            else:
                v = str(v)
            setattr(grp, k, v)
