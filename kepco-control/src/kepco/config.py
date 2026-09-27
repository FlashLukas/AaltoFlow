"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: volts (V), amperes (A), ohms, henry (H),
seconds (s), hertz (Hz).

How the Kepco BOP thinks (BIT 4886 manual, sec. 4.1.1.1) -- and so how this
config is laid out:

    The BOP has TWO control channels. The MAIN channel is what the chosen mode
    regulates (voltage in voltage mode, current in current mode). The LIMIT
    channel is the complementary quantity: the current limit in voltage mode,
    the voltage limit ("compliance") in current mode. The limit is symmetric --
    it is the ABSOLUTE value the output may reach in either polarity.

So `Output` below holds one setpoint per mode plus one limit per mode, and only
the pair that belongs to the active mode acts on the output.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

MODES = ("current", "voltage")


@dataclass
class Output:
    """The operating point. The service mirrors the LIVE values in here (so
    get_config / Save config capture what the supply is doing now).

    Nothing in here is PUSHED to the instrument at start (Lukas, 2026-09-27:
    "read the instrument state on startup, not change anything"). At start the
    brain READS the BOP's mode, main setpoint, limit and output switch and
    overwrites the active mode's values here with what it found. The values
    reach the instrument only when a user sets them (a setter, set_config or
    the Settings dialog). The other mode's setpoint and limit (which the BOP
    does not hold) stay as loaded, for when that mode is selected."""

    mode: str = "current"            # "current" or "voltage"
    current_A: float = 0.0           # main-channel setpoint in current mode
    voltage_V: float = 0.0           # main-channel setpoint in voltage mode
    voltage_limit_V: float = 10.0    # compliance in current mode (absolute value)
    current_limit_A: float = 1.0     # current limit in voltage mode (absolute value)


@dataclass
class Ramp:
    """The software ramp. An inductive load (a coil, a magnet) must never see a
    step in current: V = L dI/dt means a step asks for an infinite voltage, the
    supply slams into its limit and the coil's stored energy has to go
    somewhere. So the main channel is walked towards every new setpoint at a
    finite rate, `step_hz` times per second."""

    enabled: bool = True
    rate_A_per_s: float = 0.5        # current-mode ramp rate
    rate_V_per_s: float = 2.0        # voltage-mode ramp rate
    step_hz: float = 20.0            # how often the ramp updates the output (max 40: BIT 4886 25 ms)
    off_hold_s: float = 0.2          # wait at 0 before OUTP OFF (a coil lags by L/R)


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside it are clamped and a warn event is
    sent. The defaults are the BOP 20-10's full ratings (+-20 V, +-10 A); narrow
    them to protect a delicate load. The compliance limits are clamped to
    0 .. the larger magnitude of the matching range."""

    voltage_min_V: float = -20.0
    voltage_max_V: float = 20.0
    current_min_A: float = -10.0
    current_max_A: float = 10.0
    rate_max_A_per_s: float = 10.0
    rate_max_V_per_s: float = 40.0


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
    """The scan-safe read (`acquire`). The BIT 4886 reports the average of its
    last 16 conversions, valid ~320 ms after the output changed (manual sec.
    1.2.1). So an acquisition first waits `settle_s`, then averages `readings`
    fresh measurements."""

    settle_s: float = 0.35
    readings: int = 3
    timeout_s: float = 30.0


@dataclass
class Hardware:
    """Where the instrument lives. Only the REAL backend reads these (apart from
    the poll rate). GPIB address 6 is the BIT 4886 factory default (manual sec.
    2.2.1). On the rig this is the SAME physical BOP clMag-control drives
    (Lukas, 2026-09-27): never run the two services at once.

    full_range pins the DAC range (CURR:RANG 1 / VOLT:RANG 1) after an
    EXPLICIT mode change only -- never at start, where the range is left as
    found (start-up reads, it does not write)."""

    visa: str = "GPIB0::6::INSTR"
    visa_timeout_ms: int = 5000
    poll_hz: float = 5.0             # how often V and I are measured
    full_range: bool = True          # pin range 1 on a mode change -- no transient at 1/4 scale


@dataclass
class Sim:
    """The simulated load: a coil, i.e. a resistor in series with an inductor.
    Only the simulator reads these."""

    load_R_ohm: float = 2.0
    load_L_H: float = 0.1
    noise_V: float = 0.002           # rms measurement noise
    noise_A: float = 0.001
    # How the simulated BOP is FOUND when the service starts -- the state a
    # previous session (or clMag-control, which drives the same unit) left it
    # in. The brain adopts it and writes nothing (start-up reads only), so set
    # e.g. found_output = True and found_current_A = 1.2 to see adoption of a
    # live output. VOLT is the voltage LIMIT in current mode, CURR the current
    # limit in voltage mode (manual 4.1.1.1).
    found_mode: str = "current"
    found_output: bool = False
    found_current_A: float = 0.0
    found_voltage_V: float = 10.0


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
        self.safety = self.safety or Safety()
        self.acquisition = self.acquisition or Acquisition()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "output": Output,
        "ramp": Ramp,
        "limits": Limits,
        "safety": Safety,
        "acquisition": Acquisition,
        "hardware": Hardware,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# kepco-control configuration -- edit values, keep keys.\n")
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
    The bool case matters: bool("False") is True in Python (gotcha #3)."""
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
