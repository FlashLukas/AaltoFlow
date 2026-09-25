"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: frequencies in Hz, fields in mT
(meaning mu0*H), angles in degrees, powers in dBm, times in s.

The module drives a REAL Keysight PNA-X N5222A (`hardware` group, `--real`) or
SIMULATES one: S-parameters through a coplanar waveguide with a YIG film on top.
Next to the usual instrument groups (sweep, acquisition, hardware, limits) there
are:
  * `field`  -- where the field the sample sits in is READ from (a magnet
                service's status stream). Used in BOTH modes: in real mode it is
                the metadata every trace is filed with; in simulation it also
                drives the physics.
  * `sample`, `line` -- the PRETEND world, used only by the simulator. They are
                real settings, changeable live, because "what does a higher
                damping look like on the VNA" is a legitimate question to ask a
                simulator.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sweep:
    """What the analyser sweeps. Every one is changeable live over the wire."""

    start_Hz: float = 1.0e9
    stop_Hz: float = 6.0e9
    points: int = 1601                # 3.1 MHz spacing over 5 GHz: a YIG line is ~15 MHz wide
    ifbw_Hz: float = 10e3             # narrower = less noise, slower sweep
    power_dBm: float = -10.0          # source power; higher = less noise (the sim stays linear)
    averages: int = 1                 # sweeps averaged per acquisition (complex, coherent)
    sparam: str = "S21"               # S11 | S12 | S21 | S22: what is measured


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): `averages` sweeps that all STARTED after
    the trigger, averaged and latched as the sample."""

    timeout_s: float = 120.0          # a client gives up waiting after this
    continuous: bool = True           # sweep on its own between acquisitions (front-panel mode)


@dataclass
class Field:
    """Where the field the sample sits in is READ from (the VNA never commands it).

    source = "mag2d"  -- subscribe to the 2-axis vector magnet's status stream:
                         field = hypot(Bx, By), angle = atan2(By, Bx). The
                         DEFAULT, because that is the magnet of the VNA-FMR setup.
    source = "mag2dcal" -- the parallel 2-axis module (calibration + freeze +
                         stabilizer); identical status keys, its own ports.
    source = "clMag"  -- the 1-axis magnet: its signed measured field, angle 0.
    source = "manual" -- manual_mT at manual_angle_deg; for running with no magnet.

    It matters in real mode too: the field (and angle, and whether it was live)
    is latched into every sample, because a trace filed without the field it was
    measured in is not data.

    If the magnet stops publishing for longer than stale_s, the VNA keeps
    sweeping with the last field it saw (or the manual value if it never saw
    one) and says so: `field_ok` goes False in status and in every sample.
    """

    source: str = "mag2d"
    manual_mT: float = 50.0
    manual_angle_deg: float = 0.0
    mag2d_host: str = "127.0.0.1"
    mag2d_pub_port: int = 5576        # the vector magnet's STATUS stream
    mag2dcal_host: str = "127.0.0.1"
    mag2dcal_pub_port: int = 5578     # the calibrated vector magnet, if that one is used
    clMag_host: str = "127.0.0.1"
    clMag_pub_port: int = 5556        # the magnet's STATUS stream; the VNA never commands it
    stale_s: float = 2.0


@dataclass
class Sample:
    """The SIMULATED magnetic film on the waveguide (defaults: thin YIG, in-plane).

    ms_mT      -- saturation magnetisation mu0*Ms. YIG at room temperature: ~176 mT.
    gamma_GHz_per_T -- gyromagnetic ratio gamma/2pi; 28.0 for g = 2.
    alpha      -- Gilbert damping. Bulk YIG ~3e-5; thin films 1e-4 ... 1e-3.
    dh0_mT     -- inhomogeneous linewidth mu0*dH0 (FWHM, field units).
    h_anis_mT  -- an effective (isotropic) anisotropy field added to |H|.
    hk_mT      -- in-plane UNIAXIAL anisotropy field mu0*Hk. Default 0 = none,
                  so the numbers of the isotropic model do not move.
    easy_axis_deg -- direction of that easy axis in the film plane, in the same
                  angle convention as the magnet's field angle.
    geometry   -- "in_plane" (Kittel f = gamma*sqrt(H(H+Ms))) or
                  "out_of_plane" (f = gamma*(H - Ms); nothing below saturation).
    dip_dB     -- depth of the |S21| dip at resonance for a field 50 mT above
                  saturation. It sets the coupling; at other fields the depth
                  follows the physics (f * Im chi at resonance).
    """

    ms_mT: float = 176.0
    gamma_GHz_per_T: float = 28.0
    alpha: float = 5e-4
    dh0_mT: float = 0.3
    h_anis_mT: float = 0.0
    hk_mT: float = 0.0
    easy_axis_deg: float = 0.0
    geometry: str = "in_plane"
    dip_dB: float = 3.0


@dataclass
class Line:
    """The pretend cables + waveguide: what S21 looks like with no sample.

    A raw VNA trace is never flat: loss rises with frequency (skin effect, ~sqrt f),
    the electrical length winds the phase round many times, and small mismatches
    at the connectors make a standing-wave ripple. The FMR dip sits on top of
    all of that, which is why real data gets divided by a reference trace.
    """

    loss_dB_at_10GHz: float = 6.0
    delay_ns: float = 2.5
    ripple_dB: float = 0.15
    ripple_period_MHz: float = 350.0
    noise: float = 2e-3               # rms of complex noise at 10 kHz IFBW and -10 dBm


@dataclass
class Hardware:
    """The real analyser (used only with --real).

    visa_resource -- VISA address or alias. "N5222A" is the alias the old
                     LabVIEW program used. # VERIFY in Keysight Connection
                     Expert / NI MAX that this alias exists on the lab PC;
                     otherwise put the full "TCPIP0::<ip>::hislip0::INSTR" here.
    cal_set       -- "" = leave the instrument's correction exactly as it is;
                     otherwise the name (or {GUID}) of a calibration set to
                     activate on connect.
    timeout_s     -- VISA I/O timeout for one query. The sweep itself is waited
                     for separately, so this does not have to cover a slow sweep.
    data_format   -- "REAL,64" (binary, the default: exact and ~3x smaller) or
                     "ASCII" (what the old LabVIEW code used) as a fallback if
                     binary transfer misbehaves on the instrument.
    """

    visa_resource: str = "N5222A"
    cal_set: str = ""
    timeout_s: float = 10.0
    data_format: str = "REAL,64"


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. Loosely a 10 MHz - 20 GHz two-port VNA (the
    N5222A covers 10 MHz - 26.5 GHz, so the envelope sits inside it)."""

    freq_min_Hz: float = 10e6
    freq_max_Hz: float = 20e9
    min_span_Hz: float = 1e6
    points_min: int = 11
    points_max: int = 10001
    ifbw_min_Hz: float = 10.0
    ifbw_max_Hz: float = 1e6
    power_min_dBm: float = -60.0
    power_max_dBm: float = 10.0
    averages_min: int = 1
    averages_max: int = 1000
    manual_field_max_mT: float = 2000.0


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    sweep: Sweep = None
    acquisition: Acquisition = None
    field: Field = None
    sample: Sample = None
    line: Line = None
    hardware: Hardware = None
    limits: Limits = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sweep = self.sweep or Sweep()
        self.acquisition = self.acquisition or Acquisition()
        self.field = self.field or Field()
        self.sample = self.sample or Sample()
        self.line = self.line or Line()
        self.hardware = self.hardware or Hardware()
        self.limits = self.limits or Limits()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sweep": Sweep,
        "acquisition": Acquisition,
        "field": Field,
        "sample": Sample,
        "line": Line,
        "hardware": Hardware,
        "limits": Limits,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# vna-control configuration -- edit values, keep keys.\n")
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
