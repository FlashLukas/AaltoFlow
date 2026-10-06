"""Configuration: every tunable number in one place.

Python dataclasses with defaults, saved to / loaded from a plain-text .ini so
nothing is lost across a restart. Units are in the field names: seconds (s),
volts (V), hertz (Hz).

Two kinds of settings live here, and the difference matters:

  * the SCOPE's own settings (channel V/div, offset, coupling, probe, time/div,
    trigger): READ from the instrument at start and adopted (Lukas's rule
    2026-09-27: start changes nothing); they reach the instrument only when
    someone changes them;
  * the MODULE's settings (physical units, averaging, the filter, the loop
    analysis, the trace length): the scope knows nothing about them, they are
    applied to the traces the module reads.

Everything that shapes a RECORDED trace (points, physical units, filter) is
written into every scan's data file next to the traces.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

CHANNEL_NAMES = ("ch1", "ch2")
COUPLINGS = ("dc", "ac", "gnd")
TRIGGER_SOURCES = ("ch1", "ch2", "ext", "ext5", "line")
TRIGGER_SLOPES = ("rising", "falling")
TRIGGER_MODES = ("auto", "normal", "single", "stop")


@dataclass
class Channel:
    """One input channel.

    SCOPE settings (read at start):
      enabled        -- trace on.
      vdiv_V         -- volts per division AT THE PROBE TIP (the scope snaps
                        to its 1-2-5 steps).
      offset_V       -- vertical offset, volts at the tip.
      coupling       -- "dc", "ac" or "gnd".
      probe          -- probe attenuation (1, 10, ...); the scope already
                        divides by it, so volts here are at the probe tip.
    MODULE settings -- the physical quantity this channel measures:
      phys_scale     -- quantity per volt (e.g. mT/V for a Hall probe,
                        A/V for a shunt; 1 = stay in volts),
      phys_offset    -- quantity at 0 V,
      phys_unit      -- its unit ("mT", "A", "V", "a.u."),
      phys_label     -- what it is ("Field", "Intensity").
    quantity = phys_scale * volts + phys_offset. Traces, live view, loop and
    every number derived from them are in this unit; the conversion is
    recorded with the data.
    """

    enabled: bool = True
    vdiv_V: float = 0.5
    offset_V: float = 0.0
    coupling: str = "dc"
    probe: float = 1.0
    phys_scale: float = 1.0
    phys_offset: float = 0.0
    phys_unit: str = "V"
    phys_label: str = ""


@dataclass
class Timebase:
    """SCOPE settings (read at start).

    tdiv_s   -- time per division (snapped by the scope).
    delay_s  -- the scope's trigger delay (Siglent TRDL). 0 = the trigger in the
                centre of the record; the record's time axis is
                t = -delay - span/2 ... span/2 - delay, so a POSITIVE delay
                shows more of what happened before the trigger. # VERIFY sign"""

    tdiv_s: float = 5e-3
    delay_s: float = 0.0


@dataclass
class Trigger:
    """SCOPE settings (read at start).

    source -- "ch1", "ch2", "ext", "ext5" (the EXT input /5), "line".
    level_V, slope ("rising"/"falling"), mode ("auto", "normal", "single",
    "stop"). The module reads in whatever mode the scope is in; it changes the
    mode only when you do. A stopped scope makes no new traces, and an
    acquisition then times out saying so."""

    source: str = "ext"
    level_V: float = 0.5
    slope: str = "rising"
    mode: str = "normal"


@dataclass
class Acquisition:
    """MODULE settings: how traces are read and averaged.

    points      -- samples per recorded trace. The scope's record (often
                   thousands of points) is reduced to this many by averaging
                   neighbours (a boxcar: no aliasing, a little less noise).
                   It must not change during a scan.
    averages    -- traces per acquisition. The live view shows the mean of
                   the last `averages` traces ("312 / 500"); an acquisition
                   (one scan point) RESTARTS the average and completes when
                   that many FRESH triggered traces are in: 500 at 30 Hz is
                   ~17 s per point.
    keep_raw    -- also record the unfiltered average next to the filtered
                   one (a second trace per channel).
    timeout_s   -- the least time an acquisition may take before it fails;
                   the real limit grows with `averages` (see describe).
    min_trigger_hz -- the slowest trigger rate an acquisition plans for (sets
                   that timeout: averages / min_trigger_hz * 1.5 + 10 s).
    """

    points: int = 1000
    averages: int = 16
    keep_raw: bool = False
    timeout_s: float = 30.0
    min_trigger_hz: float = 5.0


@dataclass
class Filter:
    """MODULE settings: a digital filter on the averaged traces.

    ZERO-PHASE and IDENTICAL on every channel: a phase lag between the field
    channel and the intensity channel would tilt or open the loop. Done as a
    Butterworth magnitude |H|^2 applied in the frequency domain -- what a
    forward-backward (filtfilt) run gives, without scipy.

    lowpass_Hz / highpass_Hz -- cut-offs; 0 = off.
    order                    -- Butterworth order of ONE pass. Zero-phase means
                                two passes, so the response is |H|^2: it falls
                                like an order-2n filter (40 dB/decade per n),
                                and is -6 dB (not -3 dB) at the cut-off.
    """

    lowpass_Hz: float = 0.0
    highpass_Hz: float = 0.0
    order: int = 2


@dataclass
class Analysis:
    """MODULE settings: the hysteresis loop (Y against X) and its numbers.

    loop_x / loop_y      -- the channels for X (field or current) and Y (signal).
    sat_fraction         -- the 'high-field ends' used to fit the saturation
                            levels and the linear background: |X| above this
                            fraction of the largest |X|.
    subtract_background  -- remove the fitted linear slope (Faraday effect,
                            substrate) before the loop numbers are taken; the
                            slope is recorded either way.
    normalise            -- also give the loop scaled to -1..1.
    """

    loop_x: str = "ch1"
    loop_y: str = "ch2"
    sat_fraction: float = 0.8
    subtract_background: bool = True
    normalise: bool = False


@dataclass
class Sim:
    """The simulated bench (no instrument needed).

    scene  -- "moke": CH1 = a Hall-probe field (sine), CH2 = the light
              intensity, a hysteresis loop against it; EXT = a sync square.
              "bench": what is wired on the lab bench today -- CH1 the AFG's
              sine, CH2 and EXT its synchronous square.
    drive_Hz, field_amp_mT, hall_V_per_mT -- the drive; hc_mT, ms_V (half the
    Kerr jump), slope_V_per_mT (Faraday background), bias_mT (exchange bias),
    noise_V; drift_V_per_s (slow intensity drift).
    """

    scene: str = "moke"
    drive_Hz: float = 30.0
    field_amp_mT: float = 50.0
    hall_V_per_mT: float = 0.02
    hc_mT: float = 12.0
    bias_mT: float = 0.0
    ms_V: float = 0.2
    slope_V_per_mT: float = 0.001
    noise_V: float = 0.01
    drift_V_per_s: float = 0.0
    square_V: float = 1.0


@dataclass
class Hardware:
    """How the real instrument is reached.

    visa        -- VISA resource. The RSDS1102CML+ is a Siglent SDS1102CML+:
                   USB-TMC "USB0::0xF4EC::0xEE3A::<serial>::INSTR" (product id
                   # VERIFY), or GPIB through Siglent's USB-GPIB adapter (the
                   scope's I/O menu sets the address; it showed 18).
    timeout_ms  -- one reply; a waveform of 1.4 Mpts over GPIB takes long.
    poll_s      -- how often the trace thread asks for a new trigger when
                   none is waiting (and the status poll of the settings).
    max_points  -- the most points read per channel and trace; the scope's
                   record is thinned to about this before transfer.
    """

    visa: str = "USB0::0xF4EC::0xEE3A::SERIAL::INSTR"
    timeout_ms: int = 5000
    poll_s: float = 0.01
    max_points: int = 20000


@dataclass
class UI:
    """START-UP setting: the light/dark palette (no live toggle)."""

    theme: str = "dark"


@dataclass
class Config:
    channel_1: Channel = None
    channel_2: Channel = None
    timebase: Timebase = None
    trigger: Trigger = None
    acquisition: Acquisition = None
    filter: Filter = None
    analysis: Analysis = None
    sim: Sim = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.channel_1 = self.channel_1 or Channel(phys_label="Field")
        self.channel_2 = self.channel_2 or Channel(phys_label="Intensity")
        self.timebase = self.timebase or Timebase()
        self.trigger = self.trigger or Trigger()
        self.acquisition = self.acquisition or Acquisition()
        self.filter = self.filter or Filter()
        self.analysis = self.analysis or Analysis()
        self.sim = self.sim or Sim()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    def channel(self, ch: str) -> Channel:
        return self.channel_1 if ch == "ch1" else self.channel_2

    _GROUPS = {"channel_1": Channel, "channel_2": Channel, "timebase": Timebase,
               "trigger": Trigger, "acquisition": Acquisition, "filter": Filter,
               "analysis": Analysis, "sim": Sim, "hardware": Hardware, "ui": UI}

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# scope-control configuration -- edit values, keep keys.\n")
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
            values = {f.name: _cast(section[f.name], f.type)
                      for f in fields(klass) if f.name in section}
            kwargs[name] = klass(**values)
        return cls(**kwargs)


def _cast(raw, type_name):
    """Text from the .ini (or the wire) -> the field's type. bool("False") is
    True in Python (gotcha #3), so the text is PARSED."""
    if type_name in ("bool", bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(round(float(raw)))
    if type_name in ("float", float):
        return float(raw)
    return str(raw)
