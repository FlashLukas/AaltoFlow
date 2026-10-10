"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: frequencies in Hz, phase in degrees,
times in seconds.

What is NOT in here: the blade, reference mode, frequency, phase and run state
of the chopper. Those live in the controller itself, and the brain ADOPTS them
at start (a chopper that is already spinning for somebody's lock-in is left
spinning). The `sim` group only describes the state the SIMULATED controller
powers up in.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Blades:
    """Which blades are physically in the lab. The blade control offers only
    these (plus whatever the controller currently reports, so an unexpected
    blade is shown rather than hidden). Comma-separated so it survives the .ini.
    The MC1F10HP (10/100 slots) ships with the MC2000B-EC; the MC1F60 is the
    60-slot blade for 120 Hz - 6 kHz."""

    owned: str = "MC1F10HP, MC1F60"


@dataclass
class Limits:
    """Safety envelope ON TOP of the blade's own range. A frequency request is
    clamped to the intersection of the two (and the clamp is reported as a warn
    event). The defaults do not restrict anything the two blades on hand can do;
    tighten them if, say, a sample must never be chopped faster than 2 kHz."""

    freq_min_Hz: float = 1.0
    freq_max_Hz: float = 10_000.0
    phase_min_deg: float = 0.0
    phase_max_deg: float = 360.0          # the controller's own range (manual 8.1)
    # The frequency SWEEP (ramp_frequency, for fly scans, 2026-10-10): the
    # pace a sweep may be asked for. The wheel follows a moving synthesiser
    # through its PLL and its inertia ("locks within a few seconds", manual
    # 5.1, i.e. a time constant of a fraction of a second): far above a few
    # tens of Hz/s it lags behind the command and a lock-in referenced to it
    # sees a sliding reference. The fly scan bins by the MEASURED wheel
    # frequency, so a lag is recorded, not hidden. VERIFY on the wheel.
    sweep_rate_min_Hz_per_s: float = 0.01
    sweep_rate_max_Hz_per_s: float = 100.0


@dataclass
class Settle:
    """When is the wheel "locked" to a new frequency?

    The MC2000B has no lock query over USB (the lock indicator is on its LCD
    only), so the brain decides from what it CAN read:

      * reference OUT on a sensor ("actual" / "outer" / "inner"): the measured
        wheel frequency (refoutfreq?) must stay within the tolerance of the
        target for `hold_s`. Tolerance = max(tolerance_Hz, tolerance_rel * f).
      * reference OUT on the synthesiser ("target"): refoutfreq? only repeats
        the set value, so the wheel is unobservable and the brain waits
        `blind_lock_s` after the last change instead -- a timer, and the
        status says so (`lock_source = "timer"`).

    `timeout_s` is what a scan may wait for a lock before it gives up; the
    manual says the unit locks "within a few seconds".
    """

    tolerance_Hz: float = 0.5
    tolerance_rel: float = 0.002
    hold_s: float = 1.0
    blind_lock_s: float = 5.0
    timeout_s: float = 30.0


@dataclass
class Hardware:
    """Where the controller lives. Only the REAL backend uses these.

    The MC2000B is a USB virtual COM port (FTDI-style), 115200 8N1, commands
    terminated by CR (manual section 7.2). Find the COM number in the Windows
    Device Manager.
    """

    port: str = "COM5"
    baud: int = 115200
    timeout_s: float = 0.5                # per reply; the controller answers in ms
    poll_hz: float = 5.0                  # measured-frequency reads per second
    # ...while a frequency SWEEP runs or a fly scan records the stream (each
    # poll = enable? + refoutfreq?, a few ms at 115200 baud -- VERIFY on the
    # unit that 10 Hz leaves room for the sweep's freq= writes)
    stream_poll_hz: float = 10.0
    # one freq= write per sweep step at most every ramp_dt_s (and only when the
    # value on the synthesiser grid changes)
    ramp_dt_s: float = 0.1
    stop_on_exit: bool = False            # True: disable the motor when the service stops
    # Start-up only READS the controller (Lukas, 2026-09-27). The one write the
    # real backend used to make at connect, `verbose=0`, is now opt-in: set True
    # only if verbose status lines turn out to confuse the replies on the unit.
    quiet_on_open: bool = False


@dataclass
class Sim:
    """The state the SIMULATED controller powers up in, and its motor physics.
    A real controller keeps its own state; the brain adopts either."""

    blade: str = "MC1F10HP"
    ref_mode: str = "int-inner"
    output_mode: str = "inner"
    frequency_Hz: float = 150.0
    phase_deg: float = 0.0
    enabled: bool = True                  # a chopper found running (the harmless case)
    spinup_tau_s: float = 0.6             # first-order time constant of the PLL + motor
    coast_tau_s: float = 2.5              # spinning down after disable (no braking)
    jitter_rel: float = 2e-4              # rms relative frequency jitter when locked
    external_input_Hz: float = 0.0        # what is on EXT REF IN (0 = nothing)


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                   # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    blades: Blades = None
    limits: Limits = None
    settle: Settle = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.blades = self.blades or Blades()
        self.limits = self.limits or Limits()
        self.settle = self.settle or Settle()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "blades": Blades,
        "limits": Limits,
        "settle": Settle,
        "hardware": Hardware,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# chopper-control configuration -- edit values, keep keys.\n")
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


def _cast(raw, type_name):
    """Cast a string read from the .ini (or a JSON value off the wire) back to
    the field's declared type. The bool case is the classic trap:
    bool("False") is True, so the string must be parsed (gotcha #3)."""
    if type_name in ("bool", bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return str(raw)
