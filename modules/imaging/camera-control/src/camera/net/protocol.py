"""The wire protocol -- ports, topics, and (de)serialisation (blueprint §6).

This ONE file is shared by the service and the client so their message shapes can
never drift.  No sockets, no threads: pure data mapping.

Transport (fixed across every module in the suite):
  * Commands  : REQ/REP, JSON in -> JSON out.  Reply is always
                {"ok": true, ...} or {"ok": false, "error": "..."}.
                Fire-and-forget: ok=true means *accepted*, not *settled*.
  * Telemetry : PUB/SUB, multipart [topic, json].  Topics: b"status", b"event".

Port allocation (blueprint §6): instrument n (0-based) uses cmd = 5555 + 2n,
pub = cmd + 1.  The camera is instrument #5 -> 5563 / 5564.
"""

from __future__ import annotations

from ..camera import CameraStatus, status_to_dict  # noqa: F401  (re-export)
from ..config import Config

# -- addressing ------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5563   # REP (commands)
DEFAULT_PUB_PORT = 5564   # PUB (status + events)

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


# -- config <-> dict -------------------------------------------------------- #
def config_to_dict(cfg: Config) -> dict:
    from dataclasses import asdict
    return {
        "camera": asdict(cfg.camera),
        "image": asdict(cfg.image),
        "spot": asdict(cfg.spot),
        "pattern": asdict(cfg.pattern),
        "autofocus": asdict(cfg.autofocus),
        "scanning": asdict(cfg.scanning),
        "stabilizer": asdict(cfg.stabilizer),
        "limits": asdict(cfg.limits),
        "hardware": asdict(cfg.hardware),
        "ui": asdict(cfg.ui),
    }


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Write ``data`` INTO an existing Config in place (shared refs stay valid)."""
    groups = {
        "camera": cfg.camera,
        "image": cfg.image,
        "spot": cfg.spot,
        "pattern": cfg.pattern,
        "autofocus": cfg.autofocus,
        "scanning": cfg.scanning,
        "stabilizer": cfg.stabilizer,
        "limits": cfg.limits,
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
