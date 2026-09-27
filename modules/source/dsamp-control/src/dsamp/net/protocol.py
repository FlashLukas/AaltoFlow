"""The wire protocol shared by service and client.

Two ZeroMQ sockets, as in every module of the suite:
  * commands  -- REQ/REP, JSON in, JSON out. Every reply is {"ok": bool, ...}.
  * telemetry -- PUB/SUB, multipart [topic, json]. Topics: b"status", b"event".

Keeping the message shapes in one file means the two ends can never drift apart.

Verbs (besides the universal status/info/get_config/set_config/describe/shutdown):
    set_amp          {"on": bool}               amplifier stage on/off
    amp_off          {}                         stage off (no argument: the panic button)
    set_gain         {"gain_dB": float}         clamped to the envelope, snapped to the step
    set_frequency    {"frequency_Hz": float}    operating point, for the estimate only
    set_input_power  {"input_dBm": float}       operating point, for the estimate only
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Config

DEFAULT_CMD_PORT = 5593
DEFAULT_PUB_PORT = 5594

TOPIC_STATUS = b"status"
TOPIC_EVENT = b"event"


def status_to_dict(status) -> dict:
    """Amplifier Status dataclass -> plain dict for the wire (every field)."""
    return asdict(status)


# ---- config (Settings) over the wire ---------------------------------------

def config_to_dict(cfg: Config) -> dict:
    """The whole Config as nested plain dicts, e.g. {'amp': {...}, 'limits': {...}}."""
    return asdict(cfg)


def apply_config_dict(cfg: Config, d: dict) -> None:
    """Write values from a config dict back into an existing Config IN PLACE, so
    shared references stay valid. Unknown groups/keys are ignored (an older
    client must not crash a newer service).

    Every value is CAST to the type the field already has, and the whole dict is
    checked BEFORE anything is written. Why: a hand-typed set_config (console,
    a script) easily sends "15" instead of 15. Stored as a string, that value
    would make every later status frame fail (15.0 < "15" is a TypeError) and
    kill the publisher -- a crash that shows up minutes later, far from its
    cause. Now a bad value is refused with a clear error and the config is left
    exactly as it was. Bools are parsed, never bool()-cast (gotcha #3).
    """
    staged = []
    for group, values in d.items():
        grp = getattr(cfg, group, None)
        if grp is None or not isinstance(values, dict):
            continue
        for k, v in values.items():
            if k.startswith("_") or not hasattr(grp, k):
                continue
            staged.append((grp, k, _cast_like(getattr(grp, k), v, f"{group}.{k}")))
    for grp, k, v in staged:
        setattr(grp, k, v)


def _cast_like(current, value, name: str):
    """Convert `value` to the type of `current`; ValueError if it cannot be."""
    try:
        if isinstance(current, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(current, int):
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("not a whole number")
            return int(value)
        if isinstance(current, float):
            if isinstance(value, bool):
                raise ValueError("a bool is not a number")
            f = float(value)
            if f != f or f in (float("inf"), float("-inf")):
                raise ValueError("not a finite number")
            return f
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: cannot use {value!r} ({exc})") from None
