"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: volts (V), amperes (A), watts (W),
ohms, henry (H), seconds (s), hertz (Hz).

How the ELC ALR3203 thinks -- and so how this config is laid out:

    It is a single-output LABORATORY supply, 0..32 V, 0..6 A, at most 96 W
    (so 6 A only up to 16 V, and 3 A at 32 V). It has a voltage setpoint and a
    current setpoint, and regulates whichever one it reaches first: constant
    voltage (CV) until the current would exceed its setpoint, then constant
    current (CC). There is no "mode" command -- the mode is which of the two
    setpoints is the one you turn while the other stays out of the way.

    This module therefore has two modes, as the old LabVIEW panel had:
      voltage mode   you set the voltage; the current setpoint is the LIMIT
      current mode   you set the current; the voltage setpoint is the LIMIT
    Lukas (2026-11-09): the limit channel sits at the FULL range (32 V / 6 A),
    so the output is set by the quantity you control.

    The output itself is UNIPOLAR. A bipolar field comes from a POLARITY RELAY
    in the magnet leads, switched by a digital line of the setup's DAQ (an NI
    USB-6001 run by usb6001-control). Setpoints here are SIGNED; the brain
    programs |value| and puts the relay on the right side. See `Polarity`.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

MODES = ("current", "voltage")
RELAY_DRIVERS = ("sim", "usb6001", "none")

#: The ALR3203's own ratings (manual: 0-32 V, 0-6 A, 96 W). The programming
#: ranges in the manual's Appendix A go a little further (VOLT 0-32200 mV,
#: CURR 0-6100 mA); the limits below stay at the ratings.
RATED_V = 32.0
RATED_A = 6.0
RATED_W = 96.0


@dataclass
class Output:
    """The operating point. The service mirrors the LIVE values in here (so
    get_config / Save config capture what the supply is doing now).

    Nothing in here is PUSHED to the instrument at start (Lukas, 2026-09-27:
    "read the instrument state on startup, not change anything"). At start the
    brain READS the supply's setpoints, its output switch and the polarity
    relay, and overwrites the active mode's values here with what it found.

    current_A / voltage_V are SIGNED: a negative value means "relay inverted".
    The limits are magnitudes."""

    mode: str = "current"            # "current" or "voltage"
    current_A: float = 0.0           # setpoint in current mode (signed)
    voltage_V: float = 0.0           # setpoint in voltage mode (signed)
    voltage_limit_V: float = RATED_V  # the voltage setpoint while in current mode
    current_limit_A: float = RATED_A  # the current setpoint while in voltage mode


@dataclass
class Ramp:
    """The software ramp. An inductive load (a coil, a magnet) must never see a
    step in current: V = L dI/dt means a step asks for an infinite voltage, the
    supply slams into its limit and the coil's stored energy has to go
    somewhere. So the setpoint is walked towards every new value at a finite
    rate, `step_hz` times per second. Lukas: the magnet on this setup is small,
    so the defaults are brisk."""

    enabled: bool = True
    rate_A_per_s: float = 2.0        # current-mode ramp rate
    rate_V_per_s: float = 5.0        # voltage-mode ramp rate
    # How often the ramp updates the output. The manual gives ~20 ms of
    # processing per command, and each step also measures V and I, so 10 Hz
    # leaves the serial link room; the brain caps it at 20.
    step_hz: float = 10.0
    off_hold_s: float = 0.2          # wait at 0 before OUT OFF (a coil lags by L/R)


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside it are clamped and a warn event
    is sent. The defaults are the ALR3203's full ratings with BOTH signs: the
    negative half is reached through the polarity relay. With
    `polarity.driver = "none"` (no relay) the negative half is refused.

    power_max_W is the supply's own envelope (96 W): it is not something this
    module enforces -- the supply does -- but status says when the output sits
    on it (`power_limited`), because then the supply is not where you asked."""

    voltage_min_V: float = -RATED_V
    voltage_max_V: float = RATED_V
    current_min_A: float = -RATED_A
    current_max_A: float = RATED_A
    power_max_W: float = RATED_W
    rate_max_A_per_s: float = 10.0
    rate_max_V_per_s: float = 40.0
    # The current SWEEP (ramp_current, for fly scans) may be asked for between
    # this and rate_max_A_per_s. A sweep across zero flips the polarity relay
    # by itself at 0 (supply.ramp_current) and walks on at the same rate.
    sweep_rate_min_A_per_s: float = 1.0e-4


@dataclass
class Polarity:
    """The polarity relay that makes a unipolar supply bipolar.

    driver      "usb6001" -- the relay sits on a digital OUTPUT line of the
                usb6001-control service (this module is a CLIENT of it; the
                DAQ card belongs to that service, one card, one service).
                "sim" -- a simulated relay (default: everything runs offline).
                "none" -- no relay: the output is unipolar, negative setpoints
                are refused.
    line        the DAQ line, e.g. "p0.0" (= Dev1/port0/line0). It must be
                configured as "out" in usb6001-control.
    inverted_level  which LEVEL of that line means "inverted" (negative):
                "high" or "low". Lukas did not know yet (2026-11-09) -- confirm
                on the rig by watching the field sign.

    How a sign change happens (Lukas, 2026-11-09): the relay may switch with
    the output ON, but only with no current flowing, and there is no extra
    wait (the magnet is small). So: ramp the setpoint to 0, wait until the
    MEASURED current is below `zero_current_A`, flip the relay, ramp up on
    the other side. If the current does not fall below it within
    `zero_timeout_s`, the change is refused and the output stays at 0 --
    the relay is NEVER switched under current.

    deadband_A / deadband_V -- HYSTERESIS against chatter (Lukas: "add
    hysteresis"): with the relay on one side, a setpoint on the OTHER side
    that is smaller than this is output as 0 and does not flip the relay.
    So a setpoint dithering around zero (the end of a demag, a scan point at
    0) does not keep clicking the relay. One per mode, in that mode's unit.
    """

    driver: str = "sim"
    host: str = "127.0.0.1"          # where usb6001-control runs
    cmd_port: int = 5629             # usb6001-control's command port
    line: str = "p0.0"
    inverted_level: str = "high"     # "high" | "low"
    zero_current_A: float = 0.005    # "no current" = below this (5 mA)
    zero_timeout_s: float = 5.0
    deadband_A: float = 0.005
    deadband_V: float = 0.01
    timeout_ms: int = 1500           # one request to usb6001-control


@dataclass
class Safety:
    """What happens when things go wrong.

    shutdown_ramp_s -- the ramp to zero on shutdown is sped up (never slowed)
        so it finishes within this time. The launcher kills a service that has
        not exited 8 s after `shutdown`, and a kill mid-ramp leaves the output
        energised, so this must stay well under 8 s.
    watchdog_s -- lost-client guard. >0: if the output is on and NO command
        (a `ping` counts) has arrived for this long, ramp to zero and switch the
        output off. The GUI client pings every second; scan-core does not, so it
        is OFF (0) by default -- enable it only for GUI-driven sessions.
        It arms only once a client has switched the output on or sent a
        setpoint: an output found live at start (adopted) is never ramped down
        by the watchdog alone.
    """

    shutdown_ramp_s: float = 5.0
    watchdog_s: float = 0.0


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): wait `settle_s` after the trigger, then
    average `readings` fresh measurements of V and I (and the polarity)."""

    settle_s: float = 0.1
    readings: int = 3
    timeout_s: float = 30.0


@dataclass
class Hardware:
    """Where the instrument lives. Only the REAL backend reads these (apart from
    the poll rates).

    com         the virtual COM port of the supply's USB link ("COM5"). Empty =
                the real backend refuses to start: it never searches the ports
                (ELC's own Python library does, and writes to every COM port on
                the PC -- including other instruments'). Mission Control's
                "Instruments on this PC" offers the found ports.
    baud etc.   the ALR3203's serial settings: 9600 baud, 7 data bits, even
                parity, 1 stop bit (from ELC's ALR32XX library).
    """

    com: str = ""
    baud: int = 9600
    timeout_s: float = 1.0
    poll_hz: float = 5.0             # how often V and I are measured
    # ...and how often while a current SWEEP runs or a fly scan records the
    # stream (each measurement is two serial queries, ~20 ms each)
    stream_poll_hz: float = 10.0


@dataclass
class Sim:
    """The simulated load: a coil, i.e. a resistor in series with an inductor,
    behind the simulated polarity relay. Only the simulator reads these."""

    load_R_ohm: float = 2.0
    load_L_H: float = 0.05
    noise_V: float = 0.002           # rms measurement noise
    noise_A: float = 0.001
    # How the simulated supply is FOUND when the service starts -- the state a
    # previous session left it in. The brain adopts it and writes nothing.
    # Default: output off, set up for current mode (VOLT at full range = the
    # limit, CURR = 0), which is how the brain then reads it.
    found_output: bool = False
    found_voltage_V: float = RATED_V  # the VOLT setpoint (positive)
    found_current_A: float = 0.0     # the CURR setpoint (positive)
    found_relay_inverted: bool = False


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"              # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    output: Output = None
    ramp: Ramp = None
    limits: Limits = None
    polarity: Polarity = None
    safety: Safety = None
    acquisition: Acquisition = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.output = self.output or Output()
        self.ramp = self.ramp or Ramp()
        self.limits = self.limits or Limits()
        self.polarity = self.polarity or Polarity()
        self.safety = self.safety or Safety()
        self.acquisition = self.acquisition or Acquisition()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    # EVERY place that lists the groups reads this table (gotcha #4: a group
    # forgotten in one place silently does not travel over the wire).
    _GROUPS = {
        "output": Output,
        "ramp": Ramp,
        "limits": Limits,
        "polarity": Polarity,
        "safety": Safety,
        "acquisition": Acquisition,
        "hardware": Hardware,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        # interpolation=None: a "%" in a value must not be read as a reference
        parser = configparser.ConfigParser(interpolation=None)
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# alr3203-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser(interpolation=None)
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
    The bool case matters: bool("False") is True in Python (gotcha #3)."""
    if type_name in ("bool", bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw


#: The digital lines of a USB-6001 in usb6001-control's order: the index of a
#: line in its status list `dio` (p0.0..p0.7, p1.0..p1.3, p2.0).
DIO_LINES = tuple([f"p0.{i}" for i in range(8)] + [f"p1.{i}" for i in range(4)] + ["p2.0"])


def dio_index(line: str) -> int:
    """'p0.0' / 'P0.0' / 'port0/line0' / 'Dev1/port0/line0' -> 0."""
    s = str(line).strip().lower()
    if "port" in s:
        parts = s.split("/")
        port = [p for p in parts if p.startswith("port")]
        ln = [p for p in parts if p.startswith("line")]
        if port and ln:
            s = f"p{port[-1][4:]}.{ln[-1][4:]}"
    if s in DIO_LINES:
        return DIO_LINES.index(s)
    raise ValueError(f"unknown DAQ line {line!r} (use p0.0..p0.7, p1.0..p1.3, p2.0)")
