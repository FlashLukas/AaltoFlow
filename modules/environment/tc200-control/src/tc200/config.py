"""Configuration: every tunable number in one place.

Same idea as the other modules -- Python dataclasses with sensible defaults,
saved to / loaded from a plain-text .ini file so nothing is lost across a
restart. Units are in every field name; temperatures are degC because that is
the only unit the TC200 speaks over its serial port (its manual, section 6.3.2:
"All temperature inputs and read backs are in degC only").

Two kinds of settings live here, and the difference matters:

  * settings of THIS SOFTWARE (`temperature`, `limits`, `hardware`, `ui`):
    what counts as "reached", the safety envelope, how the port is opened.
  * settings STORED IN THE TC200 (`device`: sensor type, PID gains, PMAX,
    TMAX). The controller remembers them across power cycles and they can be
    changed on its front panel, so the software ADOPTS them at start (the
    `device` group is overwritten with what the box reports) and only PUSHES
    them on an explicit change (GUI, set_config, a verb) -- never at start.
    That way starting the service never changes how the heater behaves.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The sensor types the TC200 knows, in its own command spelling (sns=...).
SENSORS = ("ptc100", "ptc1000", "th10k")

#: Instrument ranges from the TC200 manual (section 6.3.2 command table). These
#: are the BOX's limits, not ours: our own, tighter envelope is `Limits`.
TSET_MIN_C = 20.0          # tset=nnn.n (20.0 to 200.0 to TMAX)
TSET_MAX_C = 200.0
TMAX_MIN_C, TMAX_MAX_C = 20.0, 205.0        # TMAX=nnn.n (20.0 to 205.0)
PMAX_MIN_W, PMAX_MAX_W = 0.1, 18.0          # PMAX=nn.n (0.1 to 18.0)
P_GAIN_RANGE = (1, 250)                     # pgain=nnn (1 to 250)
I_GAIN_RANGE = (0, 250)                     # igain=nnn (0 to 250)
D_GAIN_RANGE = (0, 250)                     # dgain=nnn (0 to 250)


@dataclass
class Temperature:
    """When the temperature counts as REACHED -- the flag a scan waits on.

    tolerance_C   -- |setpoint - measured| that still counts as there. The
                     manual quotes +-0.1 degC stability over 24 h and 0.1 degC
                     display resolution, so 0.2 is the tightest honest default.
    stable_time_s -- the reading must stay inside the tolerance this long,
                     continuously, before `temperature_stable` goes True. A
                     heated block overshoots and rings; 30 s rides that out.
    slowest_rate_C_per_min -- a pessimistic heating/cooling rate, used ONLY to
                     derive how long a scan may wait for one point (describe's
                     `timeout_s`). The TC200 has no ramp setting; how fast the
                     block moves depends on the heater, the PMAX and, when
                     cooling, on nothing but the room (a heater cannot cool).
    """

    tolerance_C: float = 0.2
    stable_time_s: float = 30.0
    slowest_rate_C_per_min: float = 2.0


@dataclass
class Device:
    """Settings the TC200 itself stores. ADOPTED from the box at start (these
    values are then overwritten), pushed only on request -- see the module
    docstring.

    sensor  -- "ptc100" (PT100), "ptc1000" (PT1000) or "th10k". The rig has a
               PT100. A WRONG selection is dangerous: the controller then
               misreads the temperature and can overheat the load (manual,
               chapter 4 item 8: it detects open / shorted sensors, NOT a
               mismatched type).
    p_gain, i_gain, d_gain -- the box's PID gains (unitless, 1-250 / 0-250 /
               0-250). The manual's recipe: P 125, I and D 0, then a little I
               (< 10) to remove the offset. Changing a gain drops a TUNE offset.
    pmax_W  -- output power ceiling (0.1-18 W). Set it to the heater's rating.
    tmax_C  -- the box's own over-temperature trip (20-205 degC): at or above
               it the output relay opens; the third trip disables the heater.
    """

    sensor: str = "ptc100"
    p_gain: int = 125
    i_gain: int = 5
    d_gain: int = 0
    pmax_W: float = 10.0
    tmax_C: float = 120.0


@dataclass
class Limits:
    """Our safety envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event.

    temperature_min_C -- 20 degC is the TC200's own floor (it only heats).
    temperature_max_C -- OUR ceiling for a setpoint. 100 degC is a conservative
                         default for a sample on a heater; raise it in the .ini
                         when the sample and the mount take more.
    tmax_margin_C     -- the setpoint also stays this far BELOW the box's TMAX:
                         a setpoint AT TMAX trips the relay on the first
                         overshoot (manual, troubleshooting table). So the
                         effective maximum is
                             min(temperature_max_C, TMAX - tmax_margin_C, 200).
                         TMAX is read from the box, so this limit MOVES when
                         TMAX changes (describe's revision changes with it).
    pmax_max_W        -- ceiling for set_pmax; set it to the heater's rating
                         so nobody pushes 18 W into a 5 W element.
    ramp_rate_min_C_per_s, ramp_rate_max_C_per_s -- the pace a temperature
                         SWEEP (ramp_temperature, fly scans, 2026-10-10) may
                         be asked for, in degC per SECOND (scan-core reads
                         every ramp rate as unit/s; the GUI shows K/min).
                         0.1 K/min is slower than any scan would want; 20 K/min
                         is about what 18 W lifts a small block -- the heater
                         cannot follow a faster setpoint, and it cannot COOL
                         faster than the room takes the heat away at all.
                         # VERIFY on the rig: how fast the real block follows
                         (both directions) at the PMAX in use.
    """

    temperature_min_C: float = TSET_MIN_C
    temperature_max_C: float = 100.0
    tmax_margin_C: float = 5.0
    pmax_max_W: float = PMAX_MAX_W
    ramp_rate_min_C_per_s: float = 0.1 / 60.0
    ramp_rate_max_C_per_s: float = 20.0 / 60.0


@dataclass
class Hardware:
    """How the REAL backend reaches the TC200 (used only with --real), and what
    the service does at start and stop.

    port      -- the USB virtual COM port (Windows Device Manager > Ports >
                 "USB Serial Port (COMn)"). # VERIFY on the lab PC.
    baud      -- 115200, 8N1, no flow control (manual, section 6.3.1).
    timeout_s -- how long to wait for the '>' prompt that ends every reply.
    poll_s    -- how often temperature, setpoint and status byte are read.
    settings_poll_s -- how often the STORED settings (sensor, gains, PMAX,
                 TMAX) are re-read, to notice a change made on the front panel.
    stat_base -- the number base of the stat? reply. The manual calls it "an
                 8-bit hexadecimal value"; InstrumentKit's driver parses it as
                 decimal. Bit 0 (enabled) reads the same either way; the other
                 bits do not. 16 per the manual. # VERIFY on the unit.
    expected_sensor -- what is really wired to the box. The service warns at
                 start if the box is set to anything else, and REFUSES to
                 enable the heater while it is.
    (There is no push-at-start option any more, removed 2026-09-27: start
    only READS the box. An old .ini that still has `push_on_start` loads fine;
    the key is ignored.)
    disable_on_shutdown -- switch the heater OFF when the service stops (or the
                 launcher stops it). True is the SAFER default: an unattended
                 heater stays hot with nobody watching it. False leaves it as
                 it is. (The TC200 regulates on its own, so a CRASHED service
                 leaves it heating either way -- only a clean stop can help.)
    """

    port: str = "COM5"
    baud: int = 115200
    timeout_s: float = 1.0
    poll_s: float = 0.5
    settings_poll_s: float = 5.0
    stat_base: int = 16
    expected_sensor: str = "ptc100"
    disable_on_shutdown: bool = True
    # The temperature SWEEP (ramp_temperature, 2026-10-10). The service walks
    # the setpoint (softramp.py) and sends a new `tset` every time the walk
    # has moved by the box's 0.1 degC resolution, checking every ramp_dt_s.
    # While a sweep runs -- or while a fly scan records the temperature --
    # the TEMPERATURE alone is read every ramp_poll_s (the status byte and the
    # setpoint stay at poll_s): a fly scan bins by those readings, and two a
    # second would leave short pixels empty. # VERIFY on the unit: what one
    # `tact?` costs on the serial line (100 ms assumes a few ms).
    ramp_dt_s: float = 0.1
    ramp_poll_s: float = 0.1


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    temperature: Temperature = None
    device: Device = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.temperature = self.temperature or Temperature()
        self.device = self.device or Device()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "temperature": Temperature,
        "device": Device,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# tc200-control configuration -- edit values, keep keys.\n")
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
