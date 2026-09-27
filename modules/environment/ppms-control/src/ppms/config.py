"""Configuration: every tunable number in one place.

Same idea as the other modules -- Python dataclasses with sensible defaults,
saved to / loaded from a plain-text .ini file so nothing is lost across a
restart.

Units are explicit in every field name and they are the SUITE's units, not
Quantum Design's:
    field in mT (meaning mu0*H), field rate in mT/s,
    temperature in K, temperature rate in K/min (what MultiVu shows too).
MultiVu itself speaks oersted; the conversion (1 mT = 10 Oe) happens in exactly
one place, `backends/multivu.py`, so nothing above the backend ever sees Oe.

Where the defaults come from: the old LabVIEW program
(`QDInstrument_ControlField.vi`, the DynaCool half of "QD_VNA_Integration"):
    field rate 22 mT/s, approach Linear, mode Driven;
    temperature rate 20 K/min, approach FastSettle;
    field "reached" when |set - measured| <= 0.1 mT AND MultiVu says it holds;
    temperature "reached" when |set - measured| <= 0.5 K AND MultiVu says Stable;
    plus a fixed 3 s wait after the field was reached (VNA_GUI.vi).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Field:
    """How the magnet is driven, and when the field counts as reached.

    rate_mT_per_s -- ramp rate. 22 mT/s was the old program's value.
    approach      -- "linear" | "no_overshoot" | "oscillate" (MultiVu's names).
                     linear is fastest; oscillate demagnetises on the way in.
    tolerance_mT  -- |setpoint - measured| that still counts as there.
    stable_time_s -- both conditions (inside tolerance AND MultiVu reports a
                     holding state) must hold this long before `field_stable`
                     goes True. The old program waited a fixed 3 s after
                     "reached"; this is the same pause, but it restarts if the
                     field leaves the band, so it cannot be fooled by a blip.

    There is no persistent/driven choice: the DynaCool magnet runs DRIVEN only
    (MultiPyVu: "the PPMS is the only flavor which can run persistent").
    """

    rate_mT_per_s: float = 22.0
    approach: str = "linear"
    tolerance_mT: float = 0.1
    stable_time_s: float = 3.0


@dataclass
class Temperature:
    """How the temperature is driven, and when it counts as reached.

    rate_K_per_min -- sweep rate. 20 K/min was the old program's value.
    approach       -- "fast_settle" | "no_overshoot" (MultiVu's names).
    tolerance_K    -- |setpoint - measured| that still counts as there.
    stable_time_s  -- as for the field: both conditions must hold this long.
    """

    rate_K_per_min: float = 20.0
    approach: str = "fast_settle"
    tolerance_K: float = 0.5
    stable_time_s: float = 5.0


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event.

    field_max_mT -- |B| ceiling. 9000 mT = the 9 T DynaCool magnet.
                    # VERIFY on the system: DynaCool also comes with 12 and
                    14 T magnets; set this to YOUR magnet (MultiVu refuses more
                    anyway, but a scan should be told before it tries).
    field_rate_max_mT_per_s -- the old program never went above 22 mT/s.
                    # VERIFY the system's own maximum sweep rate in MultiVu.
    temperature_min_K / max_K -- DynaCool's standard range is 1.8 - 400 K.
    temperature_rate_max_K_per_min -- the old program's 20 K/min.
    """

    field_max_mT: float = 9000.0
    field_rate_min_mT_per_s: float = 0.01
    field_rate_max_mT_per_s: float = 22.0
    temperature_min_K: float = 1.8
    temperature_max_K: float = 400.0
    temperature_rate_min_K_per_min: float = 0.01
    temperature_rate_max_K_per_min: float = 20.0


@dataclass
class Hardware:
    """How the REAL backend reaches MultiVu (used only with --real).

    MultiPyVu is Quantum Design's own Python package. It works as a small
    socket server that sits next to MultiVu and talks to it over COM, plus a
    client. This module starts that server INSIDE the service process and is
    its one client, so nothing else has to be launched -- MultiVu must just be
    running on the same PC.

    flavor   -- "DYNACOOL" (the MultiVu executable MultiPyVu should attach to).
                "" lets MultiPyVu detect which MultiVu is running.
    mpv_port -- the TCP port of MultiPyVu's own server. Bound to 127.0.0.1
                only (MultiPyVu's default is 0.0.0.0, i.e. the whole network).
                5000 is its default; change it if something else holds 5000.
    scaffolding -- MultiPyVu's built-in SIMULATION ("-s"): the real backend
                code path, with no MultiVu and no pywin32. Useful to check the
                wiring on a PC without the cryostat.
    poll_s   -- how often temperature, field and chamber are read. The old
                program used 500 ms.
    """

    flavor: str = "DYNACOOL"
    mpv_port: int = 5000
    scaffolding: bool = False
    poll_s: float = 0.5


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    field: Field = None
    temperature: Temperature = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.field = self.field or Field()
        self.temperature = self.temperature or Temperature()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "field": Field,
        "temperature": Temperature,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# ppms-control configuration -- edit values, keep keys.\n")
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

    The bool case matters: bool("False") is True, so parse the text instead.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw


#: MultiVu's approach modes, in the names MultiPyVu uses for its enums.
FIELD_APPROACHES = ("linear", "no_overshoot", "oscillate")
TEMPERATURE_APPROACHES = ("fast_settle", "no_overshoot")
