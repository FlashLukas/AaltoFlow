"""The wire protocol shared by service and client.

Two ZeroMQ sockets, the suite-wide design:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
    A reply means ACCEPTED, not done: poll status for the echo.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config, _cast

DEFAULT_CMD_PORT = 5619        # module.toml [ports]
DEFAULT_PUB_PORT = 5620

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """SignalSource Status dataclass -> plain dict for the wire."""
    return {
        "rf_on": status.rf_on,
        "frequency_Hz": status.frequency_Hz,
        "power_dBm": status.power_dBm,
        "rf_set": status.rf_set,
        "frequency_set_Hz": status.frequency_set_Hz,
        "power_set_dBm": status.power_set_dBm,
        "power_ceiling_dBm": status.power_ceiling_dBm,
        "spec_max_dBm": status.spec_max_dBm,
        "rpp_tripped": status.rpp_tripped,
        "level_unspecified": status.level_unspecified,
        "modulation_off": status.modulation_off,
        "modulation": dict(status.modulation),
        "connected": status.connected,
        "idn": status.idn,
        "hw_error": status.hw_error,
    }


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'signal': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references (the simulator holds cfg.hardware) stay valid.

    Each value is coerced to the type of the field it replaces. A bool that
    arrives as the STRING "false" would otherwise be truthy (gotcha #3), and a
    port number sent as 5.0 would become a float.
    """
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if not hasattr(grp, k):
                continue
            old = getattr(grp, k)
            if isinstance(old, bool):
                v = v if isinstance(v, bool) else _cast(str(v), "bool")
            elif isinstance(old, int):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
            setattr(grp, k, v)
