"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite-wide design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import asdict, fields

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5609
DEFAULT_PUB_PORT = 5610

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def _num(v):
    """JSON has no NaN; send None (null) for "no reading" (e.g. the measured
    frequency while the reference output is on the synthesiser)."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def status_to_dict(status) -> dict:
    """Chopper Status dataclass -> plain dict for the wire."""
    return {k: _num(v) for k, v in asdict(status).items()}


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'blades': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid (the simulator holds cfg.sim, the brain cfg).

    Every value goes through the same `_cast` as the .ini loader, so a client
    that sends "false" or 5 (for 5.0) cannot plant a wrong type (gotcha #3)."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(grp)}
        for k, v in values.items():
            if k in types:
                setattr(grp, k, _cast(v, types[k]))
