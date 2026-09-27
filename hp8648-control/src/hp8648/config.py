"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: frequencies in hertz (Hz), power in dBm,
times in seconds (s) or milliseconds (ms).

Ranges: the HP 8648D covers 9 kHz - 4000 MHz and -136 dBm up to +13 dBm (at or
below 2500 MHz) / +10 dBm (above 2500 MHz); option 1EA ("high power") raises the
ceiling further (Operation and Service Guide, chapter 4 "Specifications", and
chapter 1b "AMPLITUDE"). The FREQUENCY-DEPENDENT ceiling lives in `spec.py`;
the numbers here are YOUR envelope on top of it -- the tighter of the two wins.

A NEW CONFIG GROUP must be added in every place that lists the groups (gotcha
#4): `Config._GROUPS`, `Config.__post_init__`, `net/protocol.py`
(config_to_dict / apply_config_dict walk the dataclass, so they follow
automatically) and `apps/settings_dialog.py` (`_copy_config_into`).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Signal:
    """The CW signal pushed to the generator at start-up.

    There is deliberately NO "RF on at start-up" switch: the service always
    starts with the RF output OFF, and switching it on is always a deliberate
    command. A config file cannot then energise a sample by surprise.
    """

    frequency_Hz: float = 1_000_000_000.0   # 1 GHz
    power_dBm: float = -30.0                 # a quiet, safe default level


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside it are clamped and a `warn`
    event is emitted, so nothing silently drives the sample too hard.

    enforce_spec_ceiling -- also clamp power to the instrument's specified
        maximum at the CURRENT frequency (spec.py). With it off, only
        power_max_dBm applies and the box itself decides what to do with an
        "unspecified" level (it flags it; see status `level_unspecified`).
    """

    freq_min_Hz: float = 9_000.0             # 8648D lower end (9 kHz)
    freq_max_Hz: float = 4_000_000_000.0     # 8648D upper end (4000 MHz)
    power_min_dBm: float = -136.0            # the attenuator's bottom
    power_max_dBm: float = 13.0              # 8648D standard maximum; lower it to protect the sample
    enforce_spec_ceiling: bool = True


@dataclass
class Hardware:
    """Where the instrument lives and how we talk to it. The simulator ignores
    the VISA fields but honours option_1ea, poll_s and switch_settle_s.

    visa_resource    -- 19 is the 8648's FACTORY HP-IB address (Operation and
                        Service Guide, "HP-IB Address"). # VERIFY on the unit.
    option_1ea       -- the high-power option is fitted (raises the ceiling).
    reset_on_open    -- send *RST at connect: RF off, all modulation off, level
                        -136 dBm, every reference/offset mode off. The module
                        then pushes its own start-up signal. Turn off only if
                        you need to keep a state set up by hand on the front panel.
    poll_s           -- how often the worker thread reads the instrument back.
    switch_settle_s  -- after a frequency or level write the worker waits this
                        long before reading back, so the echo a scan waits for
                        arrives only once the synthesiser has switched (spec:
                        < 75 ms below 1001 MHz, < 100 ms above).
    """

    visa_resource: str = "GPIB0::19::INSTR"
    visa_timeout_ms: int = 5000
    option_1ea: bool = False
    reset_on_open: bool = True
    poll_s: float = 0.2
    switch_settle_s: float = 0.1


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
            fh.write("# hp8648-control configuration -- edit values, keep keys.\n")
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
    Python, because any non-empty string is truthy. So we parse the text.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
