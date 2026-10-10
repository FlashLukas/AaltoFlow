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
    """DEFAULT frequency and level -- NOT sent at start-up.

    Since 2026-09-27 (Lukas: modules read the instrument at start, they do not
    change it) the service ADOPTS whatever the generator is doing when it
    connects. These two values are sent only when you CHANGE them (Settings
    dialog OK, or set_config with a different `signal` group).

    There is deliberately NO "RF on" switch here: a config file must never
    energise a sample. RF is switched only by an explicit set_rf.
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
    # SWEEPS (ramp_frequency / ramp_power, for fly scans, 2026-10-10): the
    # pace a sweep may be asked for, in the knob's unit per second. A pace
    # outside these is clamped and warned, like a setpoint.
    #   frequency: 1 kHz/s (slower than any scan wants) .. 4 GHz/s (the whole
    #     8648D band in a second -- the steps are then large: at the default
    #     step time of 0.1 s that is 400 MHz a step. A fly scan does not mind,
    #     it bins by the value each step SENT, but every step is a synthesiser
    #     switch of up to 75-100 ms, see hardware.ramp_dt_s);
    #   power: 0.01 .. 100 dB/s. Careful with large power sweeps: the 8648
    #     switches its step attenuator at fixed levels as the level moves
    #     (POWer:ATTenuation:AUTO ON, the normal mode). If that attenuator is
    #     mechanical, it clicks -- and wears -- at every switch point, and the
    #     level may jump briefly there. VERIFY on the unit which attenuator it
    #     has and how big the glitch at a switch point is before sweeping
    #     across tens of dB. (Attenuator HOLD, set at the front panel, avoids
    #     the switching but limits the range: status `level_unspecified`.)
    ramp_rate_min_Hz_per_s: float = 1.0e3
    ramp_rate_max_Hz_per_s: float = 4.0e9
    ramp_rate_min_dB_per_s: float = 0.01
    ramp_rate_max_dB_per_s: float = 100.0


@dataclass
class Hardware:
    """Where the instrument lives and how we talk to it. The simulator ignores
    the VISA fields but honours option_1ea, poll_s and switch_settle_s.

    visa_resource    -- 19 is the 8648's FACTORY HP-IB address (Operation and
                        Service Guide, "HP-IB Address"). # VERIFY on the unit.
    option_1ea       -- the high-power option is fitted (raises the ceiling).
                        (`reset_on_open` -- *RST at connect -- was REMOVED on
                        2026-09-27: connecting must not change the instrument.
                        An old .ini that still has the key loads fine; the
                        key is ignored.)
    poll_s           -- how often the worker thread reads the instrument back.
    switch_settle_s  -- after a frequency or level write the worker waits this
                        long before reading back, so the echo a scan waits for
                        arrives only once the synthesiser has switched (spec:
                        < 75 ms below 1001 MHz, < 100 ms above).
    ramp_dt_s        -- a SWEEP sends one FREQ:CW / POW:AMPL every ramp_dt_s
                        (softramp.py), WITHOUT the switch_settle_s wait (that
                        wait is for a read-back right after a set; a sweep
                        reads nothing back). 0.1 s = 10 steps a second, chosen
                        as ONE SWITCHING TIME (spec.switching_time_s: < 75 ms
                        below 1001 MHz, < 100 ms above): a shorter step would
                        send the next frequency before the synthesiser has
                        arrived at the last one. VERIFY on the unit how long
                        one write takes over GPIB, and whether the output
                        blanks (or glitches) while it relocks -- both bound
                        how small this may be. A step that comes late does
                        not slow the sweep: each value is computed from the
                        elapsed time.
    """

    visa_resource: str = "GPIB0::19::INSTR"
    visa_timeout_ms: int = 5000
    option_1ea: bool = False
    poll_s: float = 0.2
    switch_settle_s: float = 0.1
    ramp_dt_s: float = 0.1


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
