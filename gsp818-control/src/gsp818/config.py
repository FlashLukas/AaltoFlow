"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: frequencies in Hz, levels in dBm,
attenuation and losses in dB, times in s.

The module drives a REAL GW Instek GSP-818 spectrum analyser (`hardware`
group, `--real`) or SIMULATES one. Next to the usual instrument groups
(sweep, tracking generator, acquisition, hardware, limits) there is:
  * `bench` -- the PRETEND world, used only by the simulator: which carriers
               are on the input, and what device-under-test sits between the
               tracking generator and the input. They are real settings,
               changeable live, because "what does a narrower filter look like
               through the analyser" is a legitimate question for a simulator.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sweep:
    """What the analyser sweeps and how it looks. Every one changeable live.

    The `*_auto` flags are the instrument's own couplings (RBW follows the
    span, VBW follows RBW, attenuation follows the reference level, sweep time
    follows span/RBW/VBW). Typing a value switches that coupling off, exactly
    like the front panel.
    """

    start_Hz: float = 9e3              # full span, like the instrument's preset
    stop_Hz: float = 1.8e9
    points: int = 601                  # VERIFY the instrument's allowed range / default
    rbw_Hz: float = 3e6                # resolution bandwidth (used when rbw_auto is off)
    rbw_auto: bool = True
    vbw_Hz: float = 3e6                # video bandwidth (used when vbw_auto is off)
    vbw_auto: bool = True
    ref_level_dBm: float = 0.0         # top of the screen
    atten_dB: float = 10.0             # input attenuation (used when atten_auto is off)
    atten_auto: bool = True
    sweep_time_s: float = 0.02         # used when sweep_time_auto is off
    sweep_time_auto: bool = True
    detector: str = "auto"             # auto | normal | pos_peak | neg_peak | sample
    preamp: bool = False               # 20 dB front-end preamplifier
    averages: int = 1                  # sweeps power-averaged per acquisition (by the brain)


@dataclass
class Tracking:
    """The tracking generator (option TG): a CW source that follows the sweep,
    so the analyser measures a device's transmission |S21| in dB (a SCALAR
    network analyser). OFF at every start, whatever the .ini says: it drives
    whatever is connected to GEN OUTPUT."""

    tg_on: bool = False
    level_dBm: float = -10.0           # -30 ... 0 dBm on the GSP-818


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): `sweep.averages` sweeps that all STARTED
    after the trigger, power-averaged and latched as the sample."""

    timeout_s: float = 600.0           # a client gives up waiting after this (a narrow-RBW sweep is slow)
    continuous: bool = True            # sweep on its own between acquisitions (front-panel mode)


@dataclass
class Bench:
    """The SIMULATED bench (simulator only).

    carriers   -- CW signals on the RF input, "freq_Hz:level_dBm" separated by
                  commas. A carrier narrower than a display bin is still seen by
                  a peak detector and can vanish with the sample detector --
                  exactly as on the real instrument.
    dut        -- what sits between GEN OUTPUT and RF INPUT when the tracking
                  generator is on: thru | bandpass | lowpass | open.
                  Take the reference with `thru`, then switch to the filter.
    dut_center_Hz / dut_bw_Hz -- bandpass centre and 3 dB width; for lowpass
                  the centre is the 3 dB cut-off.
    dut_order  -- Butterworth order: how steep the skirts are.
    dut_loss_dB -- passband insertion loss.
    dut_isolation_dB -- how far down the stop band bottoms out (leakage).
    cable_loss_dB_at_1GHz -- loss of the two cables, rising as sqrt(f).
    tg_ripple_dB -- flatness of the tracking generator (+-3 dB in the spec):
                  the reason a thru reference is taken and divided out.
    """

    carriers: str = "100e6:-20, 433.92e6:-45, 915e6:-62, 1575.42e6:-80"
    dut: str = "bandpass"
    dut_center_Hz: float = 900e6
    dut_bw_Hz: float = 120e6
    dut_order: int = 3
    dut_loss_dB: float = 1.5
    dut_isolation_dB: float = 70.0
    cable_loss_dB_at_1GHz: float = 1.0
    tg_ripple_dB: float = 1.0


@dataclass
class Hardware:
    """The real GSP-818 (used only with --real). Read when it connects:
    restart the service after a change.

    resource     -- VISA address. "" = search: the first USB instrument of
                    GW Instek (USB vendor id 0x2184) whose *IDN? says GSP-818.
                    Over LAN give "TCPIP0::<ip>::inst0::INSTR" (VXI-11) or
                    "TCPIP0::<ip>::<port>::SOCKET". # VERIFY which LAN
                    protocol / port the unit answers on (the manual only
                    documents setting the IP address).
    visa_library -- "" = the installed VISA (NI-VISA / Keysight IO Libraries:
                    the easy way to USBTMC on Windows); "@py" = pyvisa-py.
    timeout_s    -- VISA I/O timeout for one query.
    sweep_mode   -- how a FRESH trace is obtained:
                    "wait"   the instrument sweeps continuously; after a
                             settings change or a trigger the brain waits
                             `settle_sweeps` whole sweeps and then reads the
                             trace. Uses documented commands only (default).
                    "single" single-sweep mode (:INIT:CONT OFF) and an
                             undocumented :INIT:IMM per sweep. # VERIFY first.
    settle_sweeps -- sweeps to wait in "wait" mode (2: the one running when
                    the trigger came is not trusted, the next is whole).
    sweep_margin_s -- extra wait on top of each sweep (USB latency, retrace).
    """

    resource: str = ""
    visa_library: str = ""
    timeout_s: float = 10.0
    sweep_mode: str = "wait"
    settle_sweeps: int = 2
    sweep_margin_s: float = 0.05


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. Taken from the GSP-818 data sheet (user
    manual, Specifications)."""

    freq_min_Hz: float = 9e3
    freq_max_Hz: float = 1.8e9
    min_span_Hz: float = 100.0
    points_min: int = 11               # VERIFY on the instrument
    points_max: int = 10001            # VERIFY on the instrument
    rbw_min_Hz: float = 10.0
    rbw_max_Hz: float = 3e6
    vbw_min_Hz: float = 10.0
    vbw_max_Hz: float = 3e6
    ref_level_min_dBm: float = -80.0
    ref_level_max_dBm: float = 30.0
    atten_max_dB: float = 40.0
    sweep_time_min_s: float = 0.01
    sweep_time_max_s: float = 3000.0
    tg_level_min_dBm: float = -30.0
    tg_level_max_dBm: float = 0.0
    averages_min: int = 1
    averages_max: int = 1000


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"                # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    sweep: Sweep = None
    tracking: Tracking = None
    acquisition: Acquisition = None
    bench: Bench = None
    hardware: Hardware = None
    limits: Limits = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sweep = self.sweep or Sweep()
        self.tracking = self.tracking or Tracking()
        self.acquisition = self.acquisition or Acquisition()
        self.bench = self.bench or Bench()
        self.hardware = self.hardware or Hardware()
        self.limits = self.limits or Limits()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sweep": Sweep,
        "tracking": Tracking,
        "acquisition": Acquisition,
        "bench": Bench,
        "hardware": Hardware,
        "limits": Limits,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# gsp818-control configuration -- edit values, keep keys.\n")
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
