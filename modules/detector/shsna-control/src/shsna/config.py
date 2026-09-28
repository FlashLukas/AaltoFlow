"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: frequencies in Hz, levels in dB
(relative to the TG output -- the unit the TG44A reports),
ratios in dB, times in s.

What this module is (Lukas's decision, 2026-09-28): a SCALAR network analyser
built from the Signal Hound kit -- the USB-TG44A tracking generator sweeps a
tone through the device under test (DUT) into the SA44B / SA124B analyser,
which measures the power that comes out at the same frequency. |S21| in dB is
that power divided by the power measured with the DUT replaced by a thru (the
REFERENCE). Scalar: power only, no phase -- a spectrum analyser has no phase
reference.

The TG can only be driven through the analyser's API handle, and ONE process
may hold that handle: the `signalhound` service. So the REAL backend here is a
CLIENT of that service (`hardware` group: where it listens), and this module
never touches USB. The SIMULATOR is standalone (the `sim` group describes the
pretend chain TG -> cable -> pad -> DUT -> analyser).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sweep:
    """What one TG sweep covers. Every one is changeable live over the wire.

    `points` is what we ASK for; the analyser decides the frequency bins in the
    end (the SA API silently clamps to 1001 points), and this module files
    every trace on the grid the analyser reports, never on the one it asked for.
    `rbw_Hz` = 0 leaves the resolution bandwidth to the analyser.

    There is NO output level: measured on the lab's TG44A (2026-09-28), the
    level set with saSetTg is ignored in TG sweep mode (-30 and -20 dBm gave
    identical traces), and the trace comes back in dB RELATIVE TO THE TG's
    calibrated output, not in dBm. A knob that changes nothing would only
    mislead.
    """

    start_Hz: float = 10e6
    stop_Hz: float = 4.4e9            # the TG44A's top frequency
    points: int = 401                 # asked for; the API allows at most 1001
    rbw_Hz: float = 0.0               # 0 = the analyser's default for TG sweeps
    averages: int = 1                 # sweeps averaged per acquisition, in linear POWER


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): one TG acquisition that STARTED after
    the trigger, latched as the sample."""

    timeout_s: float = 300.0          # a scan gives up waiting after this
    # Sweep on its own between acquisitions (front-panel mode). Off by default,
    # and forced off at start on the real backend: a TG sweep is EXCLUSIVE on the
    # analyser (its spectrum display and any signal-generator CW output pause
    # while it runs), so sweeping at start would disturb another module the
    # moment this one starts (Lukas's rule, 2026-09-27: start changes nothing).
    continuous: bool = False


@dataclass
class Sim:
    """The SIMULATED measurement chain (simulator only), after the lab setup of
    2026-09-28: TG -> cable -> 20 dB attenuator -> [DUT] -> analyser.

    Everything is in dB RELATIVE TO THE TG OUTPUT (0 dB = what the TG puts
    out), as the real TG44A reports it; a thru therefore reads about
    -pad_dB - cable loss, e.g. -21 dB (the bench measured -19.4 dB through its
    20 dB pad, 900-1100 MHz). `dut_inserted` False is the THRU (what a reference is taken with);
    True puts a Butterworth band-pass in the chain. The TG's own output is not
    flat (`tg_ripple_dB`) and the cable loss rises as sqrt(f) -- exactly what
    the thru reference cancels, leaving the DUT alone.
    """

    pad_dB: float = 20.0              # the fixed attenuator between TG and analyser
    cable_loss_dB_at_1GHz: float = 1.0
    tg_ripple_dB: float = 0.6         # TG output flatness (peak)
    floor_dB: float = -110.0          # noise floor relative to the TG output, at auto RBW
    dut_inserted: bool = True
    dut_center_Hz: float = 1.0e9
    dut_bandwidth_Hz: float = 60e6    # -3 dB bandwidth of the filter
    dut_order: int = 3                # Butterworth order: skirts fall 20*order dB/decade
    dut_loss_dB: float = 1.5          # insertion loss in the pass band
    tg_attached: bool = True          # does the simulated TG44A exist?


@dataclass
class Hardware:
    """The real measurement (used only with --real): WHERE the signalhound
    service listens. That service owns the analyser and the TG; this module asks
    it for TG sweeps over its command socket and reads its status stream.

    When the launcher starts this service, AALTOFLOW_ENDPOINTS overrides these
    with the ports it really runs the signalhound module on.

    timeout_ms     -- one request to the owner; past it the owner counts as down.
    alive_s        -- the owner publishes status ~10 times a second; this long
                      without a frame means it is gone (hw_error).
    sweep_timeout_s -- give up on ONE TG acquisition after this (an owner that
                      was restarted mid-sweep forgets it, and would otherwise be
                      waited on for ever). Narrow RBW + many points are slow.
    """

    owner_host: str = "127.0.0.1"
    owner_cmd_port: int = 5587
    owner_pub_port: int = 5588
    timeout_ms: int = 2000
    alive_s: float = 2.0
    sweep_timeout_s: float = 600.0


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. The USB-TG44A: 10 Hz - 4.4 GHz (# VERIFY the
    low end on the lab unit); at most 1001 points per TG sweep (measured on
    the lab PC, 2026-09-28: the API clamps silently)."""

    freq_min_Hz: float = 10.0
    freq_max_Hz: float = 4.4e9
    min_span_Hz: float = 1e3
    points_min: int = 11
    points_max: int = 1001
    rbw_min_Hz: float = 10.0          # a set RBW below this is clamped (0 = auto stays 0)
    rbw_max_Hz: float = 250e3
    averages_min: int = 1
    averages_max: int = 1000


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    sweep: Sweep = None
    acquisition: Acquisition = None
    sim: Sim = None
    hardware: Hardware = None
    limits: Limits = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sweep = self.sweep or Sweep()
        self.acquisition = self.acquisition or Acquisition()
        self.sim = self.sim or Sim()
        self.hardware = self.hardware or Hardware()
        self.limits = self.limits or Limits()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sweep": Sweep,
        "acquisition": Acquisition,
        "sim": Sim,
        "hardware": Hardware,
        "limits": Limits,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# shsna-control configuration -- edit values, keep keys.\n")
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
