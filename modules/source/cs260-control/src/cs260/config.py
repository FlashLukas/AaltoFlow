"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: wavelengths in nanometres (nm), slit
widths in micrometres (um), times in seconds (s).

WHAT IS NOT KNOWN YET about the lab's Cornerstone 260 (ask Lukas, then edit the
.ini -- nothing else has to change):
  * how many gratings are fitted (1, 2 or 3) and their lines/mm and ranges;
  * whether the 74010 filter wheel is attached;
  * whether it is a dual-exit-port model (motorised flip mirror, OUTPORT);
  * the slit width actually fitted (fixed slits -> the bandpass readout).
The defaults describe a plain two-grating, single-port instrument with no
filter wheel, which is the configuration that cannot command hardware that is
not there.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The Cornerstone 260 turret holds at most three gratings (manual, GRAT X).
MAX_GRATINGS = 3
#: The 74010 filter wheel has six positions (manual, FILTER X).
MAX_FILTERS = 6


@dataclass
class Gratings:
    """What is on the grating turret, and the wavelength range each may use.

    lines/mm and label are also stored IN the instrument (GRATnLINES?,
    GRATnLABEL?); the real backend reads them at connect and they win for
    display. The RANGE is ours: it is the safety envelope a setpoint is clamped
    to, so it lives here where a person chose it. The manual's rule of thumb is
    a mechanical maximum of ~1600 nm for 1200 l/mm, scaling as 1/lines, and a
    usable range of 180-2500 nm overall.

    `min_nm` = 0 allows ZERO ORDER (the grating acting as a mirror: white light
    at the exit), which is what one uses to align the optics.
    """

    count: int = 2
    g1_lines: int = 1200
    g1_label: str = "VIS"
    g1_min_nm: float = 0.0
    g1_max_nm: float = 1400.0
    g2_lines: int = 600
    g2_label: str = "NIR"
    g2_min_nm: float = 0.0
    g2_max_nm: float = 2500.0
    g3_lines: int = 300
    g3_label: str = "IR"
    g3_min_nm: float = 0.0
    g3_max_nm: float = 2500.0

    def of(self, n: int) -> tuple[int, str, float, float]:
        """(lines, label, min_nm, max_nm) of grating `n` (1-based)."""
        n = int(n)
        return (int(getattr(self, f"g{n}_lines")), str(getattr(self, f"g{n}_label")),
                float(getattr(self, f"g{n}_min_nm")), float(getattr(self, f"g{n}_max_nm")))


@dataclass
class Accessories:
    """Optional hardware around the monochromator. False = not fitted: the
    control is then absent from `describe` and refused over the wire, so a scan
    cannot command a wheel that does not exist (the real instrument would answer
    with error 6, 'accessory not present')."""

    filter_wheel: bool = False
    filter_count: int = MAX_FILTERS
    # Comma-separated names for positions 1..6, for display only.
    filter_labels: str = "open,LP400,LP715,LP1000,5,6"
    # ORDER SORTING: a grating set to 800 nm also passes 400 nm in second order.
    # With auto_filter on, every wavelength move is followed by moving the
    # wheel to the filter whose band contains the new wavelength. Format:
    # "position:from-to" pairs in nm, e.g. "1:0-420,2:420-750,3:750-1100,4:1100-3000".
    auto_filter: bool = False
    filter_bands: str = "1:0-420,2:420-750,3:750-1100,4:1100-3000"
    # Dual exit ports (axial = 1, lateral = 2), selected by a flip mirror.
    dual_port: bool = False
    port_labels: str = "axial,lateral"


@dataclass
class Shutter:
    """The built-in shutter.

    There is deliberately NO "close on start" option any more (removed
    2026-09-27, Lukas's rule for every module: starting the service READS the
    instrument and changes nothing). The shutter is found as it is -- an
    experiment that left the light on keeps it on through a restart. An old
    .ini that still has `close_on_start` loads fine: unknown keys are ignored."""

    close_on_shutdown: bool = True
    # A grating change sweeps the drive PAST ZERO ORDER (white light) -- the
    # manual advises closing the shutter so a detector does not saturate. The
    # brain closes it for the change and re-opens it afterwards if it was open.
    close_during_grating_change: bool = True


@dataclass
class Motion:
    """How the brain sequences and judges moves."""

    # After GRAT n the instrument parks at grating 1's maximum or at zero order
    # (manual). Going back to the wavelength you had asked for is what a person
    # expects "change grating" to mean, so the brain does it unless told not to.
    restore_wavelength_after_grating: bool = True
    # A move is 'arrived' in the real backend when WAVE? is within this of the
    # target (the drive stops at the step closest to the request).
    arrive_tol_nm: float = 0.5
    # How often the worker polls the instrument.
    poll_s: float = 0.2


@dataclass
class Optics:
    """Only used to REPORT the bandpass (spectral resolution) with every point.

    bandpass ~ reciprocal linear dispersion x slit width. The CS260 datasheet
    gives ~6.4 nm/mm for a 1200 l/mm grating, scaling as 1200/lines. Fixed
    slits cannot be read back, so the width is typed in here."""

    slit_width_um: float = 600.0
    dispersion_nm_per_mm_at_1200: float = 6.4


@dataclass
class Limits:
    """The absolute safety envelope. Each grating's own range is intersected
    with this, so it is one place to fence off, e.g., the UV for a sample that
    must not see it. Setpoints outside are clamped and a warn event is sent."""

    wavelength_min_nm: float = 0.0
    wavelength_max_nm: float = 2500.0


@dataclass
class Hardware:
    """Where the instrument lives. Only used by the REAL backend."""

    visa: str = "GPIB0::4::INSTR"            # manual: factory GPIB address is 4
    timeout_ms: int = 3000                   # an ordinary query
    # A query sent while the drive moves may only be answered when the move has
    # finished (the instrument handles statements one at a time), so reads
    # during a move get this much longer timeout. A full 0 -> 1600 nm slew at
    # 205 nm/s is ~8 s; a grating change adds several seconds.
    move_timeout_ms: int = 30000


@dataclass
class Sim:
    """The simulator's physics (ignored by the real backend)."""

    # Datasheet: max slew 205 nm/s with a 1200 l/mm grating. The drive turns
    # the grating at a fixed angular speed, so in nm/s it scales as 1200/lines.
    slew_nm_per_s_at_1200: float = 205.0
    move_overhead_s: float = 0.15            # command parsing + motor ramp
    grating_change_s: float = 4.0
    filter_move_s: float = 1.2
    port_move_s: float = 0.6
    # Where the simulated instrument IS when the service starts -- the state
    # the brain must ADOPT (nothing is moved at start). Tests set these to
    # non-default values to prove the adoption really happens.
    start_nm: float = 532.0
    start_shutter_open: bool = True
    start_grating: int = 1
    start_filter: int = 1                    # only if a filter wheel is fitted
    start_port: int = 1                      # only if two exit ports are fitted


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    gratings: Gratings = None
    accessories: Accessories = None
    shutter: Shutter = None
    motion: Motion = None
    optics: Optics = None
    limits: Limits = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.gratings = self.gratings or Gratings()
        self.accessories = self.accessories or Accessories()
        self.shutter = self.shutter or Shutter()
        self.motion = self.motion or Motion()
        self.optics = self.optics or Optics()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "gratings": Gratings,
        "accessories": Accessories,
        "shutter": Shutter,
        "motion": Motion,
        "optics": Optics,
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
            fh.write("# cs260-control configuration -- edit values, keep keys.\n")
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

    The bool case is the classic trap (gotcha #3): bool("False") is True, so the
    TEXT has to be parsed."""
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw


def parse_labels(text: str, n: int) -> list[str]:
    """'a,b,c' -> exactly n labels (missing ones become their number)."""
    parts = [p.strip() for p in str(text or "").split(",")]
    return [(parts[i] if i < len(parts) and parts[i] else str(i + 1)) for i in range(n)]


def parse_filter_bands(text: str) -> list[tuple[int, float, float]]:
    """'1:0-420,2:420-750' -> [(1, 0.0, 420.0), (2, 420.0, 750.0)].

    Raises ValueError on a malformed entry, so a typo in the .ini is reported
    instead of silently sending light of the wrong order to the sample."""
    bands = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        pos, _, rng = item.partition(":")
        lo, _, hi = rng.partition("-")
        bands.append((int(pos), float(lo), float(hi)))
    return bands
