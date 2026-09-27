"""Configuration: every tunable number in one place.

Same idea as every module in the suite -- grouped dataclasses with sensible
defaults, saved to / loaded from a plain-text .ini file so nothing is lost
across a restart. A dataclass is just a class where you list the fields and
Python writes the boring __init__ for you.

Units are explicit in every field name: volts (V), amperes (A), ohms (ohm),
seconds (s), power-line cycles (NPLC, dimensionless).

The Keithley 2450's output envelope (datasheet, "Source" specifications) is two
overlapping boxes, not one:

    |V| <= 21 V   at  |I| <= 1.05 A
    |V| <= 210 V  at  |I| <= 105 mA

i.e. about 22 W in either corner (Keithley calls it a "20 W" SMU). A setting
outside both boxes cannot be sourced, so the brain refuses to combine a source
level and a compliance limit that together fall outside them -- see
`SourceMeter.level_limits` / `limit_limits`.

The source settings here are also the LIVE state: a setter writes the accepted
value back into cfg, so "Save config" stores what is actually in use.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Source:
    """What the SMU sources. `function` picks voltage or current; the other
    quantity is then MEASURED (source V -> measure I, source I -> measure V),
    which is how an SMU is used for IV work.

    Only the ACTIVE function's level and compliance reach the instrument; the
    other pair is remembered for when you switch.

    The OUTPUT is always OFF at start-up. There is deliberately no "output on at
    start" setting: a service restarting at 3 a.m. must not energise a sample.
    """

    function: str = "voltage"         # "voltage" or "current"
    voltage_V: float = 0.0            # source level when function = voltage
    current_A: float = 0.0            # source level when function = current
    # Compliance: the most the SMU may push of the OTHER quantity. A safe,
    # low default -- raise it deliberately for low-resistance samples.
    current_limit_A: float = 1e-3     # SCPI :SOUR:VOLT:ILIM (sourcing voltage)
    voltage_limit_V: float = 2.0      # SCPI :SOUR:CURR:VLIM (sourcing current)
    auto_range: bool = True           # source autorange
    range_V: float = 20.0             # fixed source range (V), used when auto_range is off
    range_A: float = 1e-2             # fixed source range (A), used when auto_range is off
    # How long after a new level (with the output on) the source counts as
    # "settled" for a scan. The 2450 settles its own analog loop in well under
    # a millisecond on most ranges; the time that matters is the SAMPLE's
    # (cable capacitance, a gate, a slow junction). Raise it for those.
    settle_s: float = 0.02


@dataclass
class Measure:
    """How the other quantity is measured."""

    auto_range: bool = True           # measure autorange
    range_V: float = 2.0              # fixed measure range when measuring voltage
    range_A: float = 1e-4             # fixed measure range when measuring current
    # Integration time in power-line cycles: 1 NPLC = 20 ms at 50 Hz. Longer
    # integration averages mains pickup out and lowers noise ~ 1/sqrt(NPLC).
    nplc: float = 1.0
    # 4-wire (remote sense): measure the voltage AT the sample through separate
    # sense leads, so the lead and contact resistance drop out. 2-wire includes
    # them in every voltage reading.
    four_wire: bool = False


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): average N readings that all STARTED
    after the trigger AND after the source settled, so nothing measured before
    the scan step can leak into it."""

    readings: int = 5
    timeout_s: float = 60.0           # a client gives up waiting after this


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event.

    voltage_max_V / current_max_A -- YOUR safety envelope, symmetric (+-).
        Lower them for a delicate sample; the defaults are the instrument's
        own maxima.
    box_*  -- the instrument's two output boxes (see the module docstring).
        These describe the 2450's physics; do not raise them.
    current_limit_min_A / voltage_limit_min_V -- the smallest compliance the
        2450 accepts (manual: ILIM 1 nA .. 1.05 A, VLIM 20 mV .. 210 V).
    """

    voltage_max_V: float = 210.0
    current_max_A: float = 1.05
    box_voltage_V: float = 21.0       # above this |V| ...
    box_current_A: float = 0.105      # ... |I| may not exceed this
    current_limit_min_A: float = 1e-9
    voltage_limit_min_V: float = 0.02
    nplc_min: float = 0.01
    nplc_max: float = 10.0
    readings_min: int = 1
    readings_max: int = 10000


@dataclass
class Hardware:
    """Only used by the REAL backend (VISA/SCPI); the simulator ignores all but
    line_freq_Hz and poll_hz.

    visa_resource -- USB-TMC ("USB0::0x05E6::0x2450::<serial>::INSTR"), GPIB
                     ("GPIB0::18::INSTR", the 2450's factory address) or LAN
                     ("TCPIP0::<ip>::inst0::INSTR" / "TCPIP0::<ip>::5025::SOCKET").
    visa_library  -- "" = pyvisa's default (NI-VISA / Keysight IO libraries),
                     "@py" = the pure-Python pyvisa-py (LAN works without any
                     vendor install; USB-TMC then needs pyusb + libusb).
    terminals     -- "front" or "rear" banana jacks / triax.
    """

    visa_resource: str = "GPIB0::18::INSTR"
    visa_library: str = ""
    visa_timeout_ms: int = 10000      # >= the longest single reading (10 NPLC ~ 0.2 s + autorange)
    terminals: str = "front"
    line_freq_Hz: float = 50.0        # Finland: 50 Hz. Sets how long one NPLC is.
    poll_hz: float = 20.0             # upper bound; one reading takes >= NPLC / line_freq


@dataclass
class Sim:
    """The simulator's pretend sample. Ignored by the real backend.

    load -- "resistor", "diode" (Shockley diode + series resistance, for an IV
            curve that visibly bends and trips compliance) or "open" (1 Tohm).
    lead_resistance_ohm -- total lead + contact resistance. It appears in a
            2-wire voltage reading and disappears in 4-wire, as on the bench.
    noise_ppm -- reading noise in ppm of the measure range at 1 NPLC; it falls
            as 1/sqrt(NPLC).
    """

    load: str = "resistor"
    resistance_ohm: float = 1000.0
    lead_resistance_ohm: float = 0.5
    diode_is_A: float = 1e-9            # a leaky small-signal Si diode: ~0.65 V at 1 mA
    diode_n: float = 1.8
    diode_rs_ohm: float = 5.0
    noise_ppm: float = 20.0


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    source: Source = None
    measure: Measure = None
    acquisition: Acquisition = None
    limits: Limits = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.source = self.source or Source()
        self.measure = self.measure or Measure()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "source": Source,
        "measure": Measure,
        "acquisition": Acquisition,
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
            fh.write("# k2450-control configuration -- edit values, keep keys.\n")
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

    The bool case matters (gotcha #3): bool("False") is True, so parse the text.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
