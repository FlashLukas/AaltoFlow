"""The wire protocol shared by service and client.

Two ZeroMQ sockets:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config, HallProbe
from ..calibration import FieldCalibration

DEFAULT_CMD_PORT = 5555
DEFAULT_PUB_PORT = 5556

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Controller Status dataclass -> plain dict for the wire."""
    return {
        "state": status.state,
        "setpoint_field_mT": status.setpoint_field_mT,
        "measured_field_mT": status.measured_field_mT,
        "current_A": status.current_A,
        "field_stable": status.field_stable,
        "locked": status.locked,
        "aux": status.aux,
    }


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'hall': {...}, 'pid': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the acquisition thread's hall / profiles) stay valid."""
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if hasattr(grp, k):
                setattr(grp, k, v)


# ---- calibration curve over the wire ---------------------------------------

def calibration_to_dict(cal) -> dict | None:
    """A FieldCalibration as {currents_A, fields_mT, hall{...}}, or None if empty."""
    if not cal or not getattr(cal, "currents_A", None):
        return None
    return {
        "currents_A": list(cal.currents_A),
        "fields_mT": list(cal.fields_mT),
        "hall": asdict(cal.hall),
    }


def calibration_from_dict(d: dict | None):
    """Rebuild a FieldCalibration from calibration_to_dict output (or None)."""
    if not d:
        return None
    return FieldCalibration(
        currents_A=list(d["currents_A"]),
        fields_mT=list(d["fields_mT"]),
        hall=HallProbe(**d["hall"]),
    )
