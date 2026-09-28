"""Wire protocol -- ports, topics, (de)serialisation (blueprint §6).

Instrument #6 -> cmd = 5555 + 2*5 = 5565, pub = 5566.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config
from ..config import apply_config_dict as _apply_config_dict
from ..zpiezo import ZStatus

DEFAULT_HOST = "127.0.0.1"
DEFAULT_CMD_PORT = 5565
DEFAULT_PUB_PORT = 5566

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(s: ZStatus) -> dict:
    return asdict(s)


def config_to_dict(cfg: Config) -> dict:
    return {"limits": asdict(cfg.limits), "hardware": asdict(cfg.hardware)}


# ONE implementation (cast + envelope check, all or nothing) lives in config.py;
# re-exported here because clients of the old layout import it from protocol.
apply_config_dict = _apply_config_dict
