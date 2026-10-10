"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite's usual design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5625
DEFAULT_PUB_PORT = 5626

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Generator Status dataclass -> plain dict for the wire.

    `hw_error` is the suite-wide key (docs/DEVELOPER_NOTES.md section 4): a
    non-empty string means "the values in this frame are not the TG's", and a
    scan never accepts such a frame as settled. `tg_busy` is this module's own:
    a network-analyser sweep holds the TG, so the CW is not being delivered and
    commands are refused. `tg_unknown`: the owner cannot read the TG's state
    (it may be emitting); the three values are then not the TG's. `tg_ready`
    folds all of it into the one flag a scan's settle rule waits on.
    `parked`: the TG44A cannot be silenced, so "off" parks it at `park_Hz`,
    `park_dBm`; `rf_on` is False then, but the TG is NOT silent.
    The sweeps' flat keys (`ramping`, `frequency_ramp_id`, ...) go on the
    top level, because a ramp block's `done` names them as plain status keys.
    """
    d = {
        "rf_on": status.rf_on,
        "power_dBm": status.power_dBm,
        "frequency_Hz": status.frequency_Hz,
        "connected": status.connected,
        "idn": status.idn,
        "parked": status.parked,
        "park_Hz": status.park_Hz,
        "park_dBm": status.park_dBm,
        "tg_busy": status.tg_busy,
        "tg_unknown": status.tg_unknown,
        "tg_ready": status.tg_ready,
        "hw_error": status.hw_error,
    }
    d.update(getattr(status, "sweep", None) or {})
    return d


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'signal': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if hasattr(grp, k):
                setattr(grp, k, v)
