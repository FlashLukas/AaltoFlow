"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name:
    wavelengths in nanometres (nm), power level and RF amplitude in percent (%),
    times in seconds (s), temperatures in degrees Celsius (C).

Per-line and per-filter values are stored as COMMA-SEPARATED strings
("650,700,0,..."), the same trick clMag uses for its AUX channel lists: a plain
string survives the .ini file and the JSON wire unchanged, and the brain parses
it with `floats()` / `ints()` / `names()` below.

THE SAFETY IDEA of this file: the SuperK EXTREME is a class 4 laser. Nothing in
here can make it emit at start-up -- there is deliberately NO "emission at
start" field -- and the power level every request is clamped to has its own
ceiling (`limits.power_max_pct`) that you raise on purpose, not by accident.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The SuperK SELECT RF driver has 8 independent RF channels = 8 lines that can
#: be diffracted out of the white light at once (register blocks 0x90-0x97 for
#: the wavelength, 0xB0-0xB7 for the amplitude).
N_LINES = 8


@dataclass
class Startup:
    """What the service pushes to the hardware when it starts.

    Emission is NOT here on purpose: the laser never starts emitting because a
    service started. The RF output also starts OFF.
    """

    power_pct: float = 10.0                    # EXTREME power level written at start
    filter: str = "VIS-nIR"                    # which AOTF crystal is driven at start
    # line 1 is the "scan" line; lines 2..8 start at 0 % amplitude = not emitting
    wavelengths_nm: str = "650,700,750,800,850,600,550,520"
    amplitudes_pct: str = "80,0,0,0,0,0,0,0"


@dataclass
class Limits:
    """Hard safety envelope. Every request is clamped to it, and a clamp is
    announced as a warn event -- nothing is silently changed.

    power_max_pct -- the EXTREME's power level ceiling. Deliberately below 100:
                     raise it in the .ini when the experiment needs it.
    amplitude_max_pct -- RF amplitude ceiling per AOTF line. Above the crystal's
                     saturation point more RF does not give more light (the
                     diffraction efficiency goes over the top of its sin^2),
                     it only heats the crystal.
    """

    power_min_pct: float = 0.0
    power_max_pct: float = 50.0
    amplitude_max_pct: float = 100.0


@dataclass
class Filters:
    """The AOTF crystals the ONE RF driver can drive.

    Lab setup: a SuperK SELECT with a VIS-nIR and an nIR2 crystal, and a SuperK
    SELECT2 with only an IR crystal, all fed by a single RF driver -- so exactly
    ONE crystal is active at a time, and the allowed wavelength range of every
    line follows that choice.

    names       -- labels shown in the GUI and used on the wire (set_filter)
    min_nm/max_nm -- the tuning range of each crystal (NKT datasheet classes:
                  VIS-nIR 500-900, nIR2 800-1400, IR 1100-2000). The real
                  backend reads the range from the RF driver after selecting a
                  crystal and that reading wins (# VERIFY registers 0x34/0x35).
    crystal     -- NKT's number for each crystal, as the RF driver reports it
                  in its read-only "connected crystal" register (75h): 1 and 2
                  are the two slots of the SELECT housing with the LOWEST bus
                  address, 3 and 4 the slots of the next housing. "-/IR" means
                  SELECT2's slot 1 is empty and IR sits in slot 2, hence 4 --
                  IF SELECT2 has the higher address (# VERIFY with NKT CONTROL;
                  if it is the lower one the table is "3,4,2").
                  Within one housing the backend switches crystals itself (the
                  SELECT's RF switch); a crystal in the OTHER housing needs the
                  RF cable moved by hand, and the backend says so.
    """

    names: str = "VIS-nIR,nIR2,IR"
    min_nm: str = "500,800,1100"
    max_nm: str = "900,1400,2000"
    crystal: str = "1,2,4"


@dataclass
class Hardware:
    """Where the instrument lives. Only used by the REAL backend; the simulator
    ignores most of it.

    The SuperK is an Interbus system: one serial (USB) port, several MODULES on
    it, each with its own address. The addresses below are NKT's usual ones but
    they are system-specific (# VERIFY with NKT CONTROL, or leave `autodetect`
    on so the backend finds them by their module-type code).
    """

    port: str = "COM3"                 # the EXTREME's USB virtual COM port
    dll_path: str = ""                 # "" = find NKTPDLL.dll via NKTP_SDK_PATH
    autodetect: bool = True            # find modules by type code (0x60 / 0x66)
    extreme_addr: int = 15             # SuperK EXTREME main module (0x0F)
    rf_addr: int = 16                  # SuperK SELECT RF driver
    # The EXTREME's own watchdog switches emission OFF when it hears nothing
    # for this many seconds -- the only protection that still works when the
    # service process is KILLED (no code runs then). 0 disables it.
    watchdog_s: int = 10
    poll_hz: float = 5.0               # how often the worker re-reads the hardware
    # Switch emission off when the service starts (in case someone left it on
    # from the front panel). Safe default; see the open questions in CLAUDE.local.md.
    emission_off_on_start: bool = True
    # Sim only: seconds from "emission on" until the laser reports emitting.
    sim_warmup_s: float = 1.5


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    startup: Startup = None
    limits: Limits = None
    filters: Filters = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.startup = self.startup or Startup()
        self.limits = self.limits or Limits()
        self.filters = self.filters or Filters()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    # A new group must ALSO be added in __post_init__ above and in the settings
    # dialog's group list (docs/DEVELOPER_NOTES.md gotcha #4). protocol.py uses
    # asdict(), so it follows this class automatically.
    _GROUPS = {
        "startup": Startup,
        "limits": Limits,
        "filters": Filters,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# superk-control configuration -- edit values, keep keys.\n")
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
    string has to be parsed (docs/DEVELOPER_NOTES.md gotcha #3).
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw


# ---- comma-list helpers ------------------------------------------------------

def names(text: str) -> list[str]:
    """'VIS-nIR, nIR2,IR' -> ['VIS-nIR', 'nIR2', 'IR'] (empty items dropped)."""
    return [t.strip() for t in str(text).split(",") if t.strip()]


def floats(text: str, n: int | None = None, fill: float = 0.0) -> list[float]:
    """'650,700' -> [650.0, 700.0]; padded/truncated to n items when n is given,
    so a short list in a hand-edited .ini cannot crash the brain."""
    out = []
    for t in str(text).split(","):
        t = t.strip()
        if t:
            try:
                out.append(float(t))
            except ValueError:
                out.append(fill)
    if n is not None:
        out = (out + [fill] * n)[:n]
    return out


def ints(text: str, n: int | None = None, fill: int = 1) -> list[int]:
    return [int(round(v)) for v in floats(text, n, float(fill))]


def join(values) -> str:
    """[650.0, 700.0] -> '650,700' (the inverse of floats())."""
    return ",".join(f"{v:g}" for v in values)
