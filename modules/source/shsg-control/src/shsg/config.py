"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class
where you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: frequencies in hertz (Hz), level in dBm.

What this module drives: the Signal Hound USB-TG44A TRACKING GENERATOR, used as
a plain CW source. The TG has no phase control and no modulation, so there is
no phase here. Its range (10 Hz .. 4.4 GHz, -30 .. -10 dBm) is the default
safety envelope below -- # VERIFY both ends on the rig (the numbers come from
the datasheet, not from a measurement).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Signal:
    """Signal DEFAULTS. They are NOT pushed at start: the service adopts
    whatever the tracking generator is already doing (Lukas's adopt-on-start
    rule). A default is applied only when the user changes it (Settings dialog
    / set_config). The SIMULATOR also uses this group as the state its fake TG
    is already in when we connect, so adoption is exercised for real."""

    frequency_Hz: float = 1_000_000_000.0    # 1 GHz
    power_dBm: float = -20.0                 # middle of the TG's -30..-10 dBm
    rf_on: bool = False                      # simulator only: the fake TG's state


@dataclass
class Limits:
    """Hard safety envelope. A setpoint outside it is CLAMPED and the clamp is
    reported as a warning event. The defaults are the TG44A's own range; narrow
    them to protect a sensitive sample (widening them past the hardware only
    gets the command refused by the analyser service)."""

    freq_min_Hz: float = 10.0                # VERIFY: TG44A lower end
    freq_max_Hz: float = 4_400_000_000.0     # VERIFY: TG44A upper end (4.4 GHz)
    power_min_dBm: float = -30.0             # VERIFY: TG44A minimum level
    power_max_dBm: float = -10.0             # VERIFY: TG44A maximum level (tested: -30, -20)
    # SWEEPS (ramp_frequency / ramp_power, for fly scans, 2026-10-10): the pace
    # a sweep may be asked for, in the knob's unit per second. A pace outside
    # these is clamped and warned, like a setpoint.
    #   frequency: 1 kHz/s .. 1 GHz/s. The top end crosses the whole TG band
    #     in under 5 s; with one step every hardware.ramp_dt_s (100 ms) each
    #     step is then 100 MHz -- a fly scan does not mind (it bins by the
    #     value each step SENT), but whatever detects the tone must keep up.
    #     VERIFY the smallest frequency step saSetTg honours: at the bottom
    #     end (1 kHz/s) a step is only 100 Hz;
    #   level: 0.01 .. 20 dB/s (the TG's whole -30..-10 dBm range in 1 s).
    #     VERIFY on the rig how the TG44A changes level: if it switches an
    #     attenuator at fixed points, the tone may jump or dip there.
    ramp_rate_min_Hz_per_s: float = 1.0e3
    ramp_rate_max_Hz_per_s: float = 1.0e9
    ramp_rate_min_dB_per_s: float = 0.01
    ramp_rate_max_dB_per_s: float = 20.0


@dataclass
class Hardware:
    """Where the tracking generator is reached (REAL backend only).

    The TG hangs off the spectrum analyser's USB handle: the vendor API can
    only drive it THROUGH the analyser. So the only process that may touch it
    is the signalhound service (the owner of both USB devices), and this module
    is a CLIENT of that service. These fields say where it listens. When the
    launcher starts us, AALTOFLOW_ENDPOINTS overrides host and ports (see
    shsg/endpoints.py), so a port changed in the launcher reaches us too.
    """

    owner_host: str = "127.0.0.1"
    owner_cmd_port: int = 5587               # signalhound commands (REQ/REP)
    owner_pub_port: int = 5588               # signalhound status (PUB/SUB)
    owner_timeout_ms: int = 1500             # one command round trip
    owner_wait_s: float = 5.0                # at start: how long to wait for its first frame
    # PARK the TG when THIS service stops cleanly ("off" = park: the TG44A
    # cannot be silenced, it keeps emitting its last frequency and level).
    # Lukas, 2026-09-28: this service is the one that switches the CW on, so
    # it parks it on its own clean exit. A killed service leaves the TG as it
    # is (the signalhound service still owns it and parks it when IT stops).
    off_on_shutdown: bool = True
    # How far the analyser's echo may sit from what we asked for before a scan
    # accepts it as "applied". The API may round the frequency or the level to
    # its own grid. # VERIFY on the rig: set odd values, compare the echo.
    echo_tol_Hz: float = 1.0
    echo_tol_dB: float = 0.05
    # A SWEEP sends one tg_cw (frequency or level) every ramp_dt_s
    # (softramp.py). 100 ms = 10 steps a second -- twice smb's 50 ms, because
    # every step here is a ZeroMQ round trip to the signalhound service PLUS
    # that service's USB call: saSetTg took ~0.03 s on the lab's SA44B + TG44A
    # (measured 2026-09-28), and the owner first waits (up to 0.2 s) for its
    # hardware lock if it is in the middle of fetching a spectrum. 100 ms
    # leaves about 3x headroom over the measured call. VERIFY on the rig that
    # the steps keep this pace (the stream's time stamps show it) while the
    # analyser also sweeps spectra. A late step does not slow the sweep: each
    # value is computed from the elapsed time, a late one is just further on.
    ramp_dt_s: float = 0.1


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle). Persisted
    in the .ini and carried over the wire so a remote GUI matches the service."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    signal: Signal = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.signal = self.signal or Signal()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "signal": Signal,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# shsg-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        kwargs = {}
        for name, klass in cls._GROUPS.items():
            if name not in parser:
                continue
            section = parser[name]
            values = {}
            for f in fields(klass):
                if f.name not in section:
                    continue
                # with `from __future__ import annotations`, f.type is a string
                # like "float"/"bool", so we always route through _cast.
                values[f.name] = _cast(section[f.name], f.type)
            kwargs[name] = klass(**values)
        return cls(**kwargs)


def _cast(raw: str, type_name):
    """Cast a string read from the .ini back to the field's declared type.

    The bool case is the classic trap: bool("False") is True in Python, so the
    TEXT has to be parsed (docs/DEVELOPER_NOTES.md gotcha #3)."""
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
