"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name:
    frequencies in hertz (Hz), power in dBm, phase in degrees (deg).

Two envelopes, and why there are two
------------------------------------
The SG12000L reports its OWN range (FREQ:MIN?/FREQ:MAX?, POWER:MIN?/POWER:MAX?).
`Limits` below is YOUR safety envelope on top of that. The brain clamps to the
INTERSECTION of the two, so a unit with a narrower range than the config can
never be commanded outside what it can do, and a conservative config ceiling can
never be exceeded just because the box could go higher. `describe` publishes that
intersection, so a scan sees the real, live range.

Start-up ADOPTS, it never pushes (Lukas's rule, 2026-09-27: "all modules
should read the instrument state on startup, not to change anything"). When the
service connects it READS what the SG12000L is doing -- RF on/off, frequency,
power, phase, reference -- and shows that. Nothing in this file is sent to the
unit at start. The `signal` group is a PRESET: it is sent only when you change
it (Settings dialog, set_config, loading an .ini). There is deliberately no
"RF on" setting at all.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The three reference choices the SG12000L understands (*INTERNALREF 1 / 0 / A).
REFERENCES = ("internal", "external", "auto")


@dataclass
class Signal:
    """A PRESET signal. It is NOT sent at start (the module adopts whatever the
    unit is doing); a field is sent to the generator only when it CHANGES here
    -- in the Settings dialog, over set_config, or by loading an .ini. The RF
    output is never switched by a preset."""

    frequency_Hz: float = 1_000_000_000.0    # 1 GHz
    power_dBm: float = -20.0                  # a quiet, safe default level
    phase_deg: float = 0.0
    reference: str = "auto"                   # internal | external | auto (10 MHz ref)


@dataclass
class Limits:
    """Your safety envelope. Setpoints outside it are clamped and a warning
    event is sent. The instrument's own range (read at connect) narrows it
    further; the brain always uses the intersection.

    power_max_dBm is deliberately BELOW the SG12000L's calibrated maximum
    (+10 dBm): raise it on purpose, not by accident.
    """

    freq_min_Hz: float = 25_000_000.0         # 25 MHz -- the SG series' bottom end
    freq_max_Hz: float = 13_000_000_000.0     # the shop page says 13 GHz; the box reports its own
    power_min_dBm: float = -40.0
    power_max_dBm: float = 5.0
    phase_min_deg: float = 0.0
    phase_max_deg: float = 360.0
    # VERNIER fine power trim, in raw integer counts (no unit). The vendor
    # documents neither the range nor the dB per count (its examples are 3
    # and -22), so +-30 is a SAFE GUESS, not a datasheet value: widen it once
    # the counts -> dB curve has been measured on the unit (VERIFY).
    vernier_min: int = -30
    vernier_max: int = 30


@dataclass
class Hardware:
    """How to reach the real instrument (the simulator ignores the transport).

    transport = "serial": the USB-C port shows up in Windows as a virtual COM
                port; 115200 baud, 8N1, no flow control (DSI command list).
    transport = "tcp":    the Ethernet option; a raw TCP socket on port 10001
                ("fixed for all DSI models", DSI Ethernet app note V1.2). The
                unit gets its address by DHCP unless you set a static one.
    """

    transport: str = "serial"                 # "serial" | "tcp"
    com_port: str = "COM5"                    # see Device Manager > Ports (COM & LPT)
    baud: int = 115200
    host: str = ""                            # IP address of the unit (tcp only)
    tcp_port: int = 10001
    timeout_s: float = 1.0                    # per query
    poll_hz: float = 5.0                      # how often the worker reads the box back
    # The calibrated step attenuator moves in 0.5 dB steps (datasheet). The
    # read-back power may therefore differ from the request by up to half a
    # step, and the scan's echo tolerance is derived from this number.
    power_step_dB: float = 0.5
    # How close FREQ:CW? must be to the request to count as "arrived". The
    # fractional-N synthesiser's grid is up to ~3 kHz coarse; whether the query
    # returns the request or the grid value is not documented (VERIFY). If it
    # returns the grid value, the read-back can be up to HALF a grid step
    # (~1.5 kHz) away from the request, so the tolerance must be above that --
    # a tolerance below it makes a scan wait forever for a value the box can
    # never report. 2 kHz is ~1e-6 of 2 GHz: far below any FMR linewidth.
    freq_echo_tol_Hz: float = 2000.0
    # Same idea for PHASE?: the phase resolution of the firmware is not
    # documented (VERIFY). 0.5 deg keeps a scan from hanging if the unit rounds
    # to whole degrees, and is still finer than any sensible phase step.
    phase_echo_tol_deg: float = 0.5
    # Phase control: "auto" probes PHASE? at connect (the 2022 SG12000L command
    # list has no PHASE command, the shop page and the DSI app note do), "on"
    # forces it, "off" hides it.
    phase_mode: str = "auto"
    # Front-panel preferences. NOT sent at start any more (adopt-on-start
    # rule): *BUZZER / *DISPLAY are written only when you CHANGE the value
    # (Settings / set_config). Default False = leave the unit as it is. Until
    # 2026-09-27 mute_buzzer defaulted to True and was pushed on every connect.
    mute_buzzer: bool = False                 # True -> *BUZZER OFF (it beeps on every change)
    display_off: bool = False                 # True -> *DISPLAY OFF (faster commands, DSI app note);
                                              # switched back ON at disconnect if WE turned it off


@dataclass
class Sim:
    """What the SIMULATED SG12000L reports about itself. Only used without --real.
    The defaults follow the SG series datasheet (V3.6, Dec 2022)."""

    freq_min_Hz: float = 25_000_000.0
    freq_max_Hz: float = 12_000_000_000.0
    power_min_dBm: float = -21.5              # calibrated range
    power_max_dBm: float = 10.0
    has_phase: bool = True
    has_vernier: bool = True                  # the simulated firmware answers VERNIER?
    external_ref_present: bool = False        # is a 10 MHz cable plugged into the MCX jack?
    # The state the simulated box is IN when the service connects, as if
    # someone had left it like that from the front panel. The module adopts
    # it (never overwrites it), so this is what the GUI shows at start.
    # Deliberately not the `signal` preset's values, so adoption is visible.
    state_rf_on: bool = False
    state_frequency_Hz: float = 2_450_000_000.0
    state_power_dBm: float = -10.0
    state_phase_deg: float = 0.0
    state_vernier: int = 0
    state_reference: str = "internal"


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    signal: Signal = None
    limits: Limits = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.signal = self.signal or Signal()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    # EVERY group must be listed here (gotcha #4): a group missing from this
    # table is silently not saved, not loaded and not sent over the wire.
    _GROUPS = {
        "signal": Signal,
        "limits": Limits,
        "hardware": Hardware,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# dssg-control configuration -- edit values, keep keys.\n")
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

    The bool case is the classic trap (gotcha #3): bool("False") is True in
    Python, so the text has to be parsed, not converted."""
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
