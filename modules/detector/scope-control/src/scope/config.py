"""Configuration: every tunable number in one place.

Python dataclasses with defaults, saved to / loaded from a plain-text .ini so
nothing is lost across a restart. Units are in the field names: seconds (s),
volts (V), hertz (Hz).

Two kinds of settings live here, and the difference matters:

  * the SCOPE's own settings (channel V/div, offset, coupling, probe, time/div,
    trigger): READ from the instrument at start and adopted (Lukas's rule
    2026-09-27: start changes nothing); they reach the instrument only when
    someone changes them;
  * the MODULE's settings (physical units, averaging, the filter, the trace
    length): the scope knows nothing about them, they are applied to the
    traces the module reads.

Everything that shapes a RECORDED trace (points, physical units, filter) is
written into every scan's data file next to the traces.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

CHANNEL_NAMES = ("ch1", "ch2")
COUPLINGS = ("dc", "ac", "gnd")
# Every trigger source any backend offers; each backend says which of them
# it HAS (capabilities "trigger_sources"): Siglent ch1/ch2/ext/ext5/line,
# Analog Discovery ch1/ch2, ext1/ext2 (its T1/T2 pins) and w1/w2 (its own
# generator outputs starting).
TRIGGER_SOURCES = ("ch1", "ch2", "ext", "ext5", "line", "ext1", "ext2", "w1", "w2")
SIM_MODELS = ("sds", "ad")
DRIVERS = ("siglent", "dwf")
TRIGGER_SLOPES = ("rising", "falling")
# when the first record of an acquisition counts (Acquisition.freshness)
FRESHNESS = ("strict", "trigger")
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
      phys_scale     -- quantity per volt (e.g. 10 A/V for a current probe
                        that gives 0.1 V/A; 1 = stay in volts),
      phys_offset    -- quantity at 0 V,
      phys_unit      -- its unit ("A", "mT", "V", "a.u."),
      phys_label     -- what it is ("Current", "Signal").
    quantity = phys_scale * volts + phys_offset. Traces, live view and every
    number derived from them are in this unit; the conversion is recorded
    with the data.
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
                t = delay - span/2 ... delay + span/2, so a POSITIVE delay
                moves the window LATER (more of what follows the trigger);
                the trigger stays at t = 0. Measured on the lab scope
                2026-10-07."""

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
    freshness   -- when the FIRST record of an acquisition counts:
                   "strict" (default): the whole record, pre-trigger part
                   included, was recorded after the acquisition started;
                   "trigger": its TRIGGER came after it -- the part after
                   the trigger is new, the part BEFORE the trigger may predate
                   the request. Faster at a slow time/div (0.5 s/div, delay 0:
                   about half a 16 s record sooner); with a positive delay
                   (trigger near the record start) the stale part is small.
    """

    points: int = 1000
    averages: int = 16
    keep_raw: bool = False
    timeout_s: float = 30.0
    min_trigger_hz: float = 5.0
    freshness: str = "strict"


@dataclass
class Filter:
    """MODULE settings: a digital filter on the averaged traces.

    ZERO-PHASE and IDENTICAL on every channel, so the channels stay
    time-aligned with each other (an ordinary filter delays a signal by a
    frequency-dependent amount, which would shift one channel against the
    other -- and distort an XY plot). Done as a Butterworth magnitude |H|^2
    applied in the frequency domain -- what a forward-backward (filtfilt) run
    gives, without scipy.

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
class Sim:
    """The simulated bench (no instrument needed): two test signals and a sync.

    CH1  a sine: frequency_Hz, ch1_amplitude_V (peak);
    CH2  the same frequency, ch2_amplitude_V (peak), shifted by ch2_phase_deg
         and with a ch2_harmonic share of its 2nd harmonic, plus ch2_offset_V
         -- so the XY view is a tilted, slightly distorted ellipse, not a line;
    EXT  a sync square (0 / square_V), high for the first half period;
    noise_V rms on both channels, fresh in every record.
    """

    frequency_Hz: float = 30.0
    ch1_amplitude_V: float = 1.0
    ch2_amplitude_V: float = 0.2
    ch2_phase_deg: float = 60.0
    ch2_harmonic: float = 0.15
    ch2_offset_V: float = 0.5
    noise_V: float = 0.01
    square_V: float = 1.0
    # which instrument the simulator pretends to be: "sds" (the Siglent
    # bench above) or "ad" (an Analog Discovery 2: its generator W1/W2 looped
    # back to its scope CH1/CH2, as on the lab bench, plus V+/V- supplies --
    # the signals then come from the generator, not from the fields above)
    model: str = "sds"


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
    roll_tdiv_s -- in AUTO trigger mode at this time/div and slower the scope
                   free-runs / rolls and no triggered records come (lab PC
                   2026-10-07: at 0.5 s/div in AUTO one record in 120 s). An
                   acquisition is refused THERE (AUTO + slow), with the
                   reason: switch to NORMAL. In NORMAL mode triggered records
                   keep coming at every time/div, just slowly (measured: one
                   per ~8 s at 0.1 - 0.5 s/div), and the acquisition simply
                   waits long enough. # VERIFY where AUTO starts rolling.
                   (The Analog Discovery does not roll: not used there.)
    driver      -- the real instrument behind --real: "siglent" (the RS PRO
                   RSDS1102CML+, over VISA) or "dwf" (a Digilent Analog
                   Discovery 2 / 3 through the WaveForms runtime's dwf library).
    dwf_device  -- which Analog Discovery: its serial number, "#<n>" for
                   the n-th one WaveForms lists, or empty = the first one that
                   is free. (A serial is this PC's business: it stays in
                   scope.ini, never in tracked files.)
    dwf_trigger_hysteresis_div -- the Analog Discovery's trigger hysteresis,
                   in divisions of the source channel (0.05 = 31 mV at the 5 V
                   range). Without it the input noise re-triggers on the wrong
                   edge (lab AD2 2026-10-08: the slope looked ignored).
    """

    driver: str = "siglent"
    visa: str = "USB0::0xF4EC::0xEE3A::SERIAL::INSTR"
    dwf_device: str = ""
    dwf_trigger_hysteresis_div: float = 0.05
    timeout_ms: int = 5000
    poll_s: float = 0.01
    max_points: int = 20000
    roll_tdiv_s: float = 0.05


@dataclass
class Supplies:
    """The SAFETY limits of an instrument's power supplies (the Analog
    Discovery's V+ / V-). A request beyond them is clamped (with a warn
    event); the device's own range narrows them further (AD2: 0..+5 V and
    -5..0 V). The supplies are never switched on at start, and are switched
    OFF when the service stops (unless it is a restart that keeps outputs).

    vplus_max_V   -- highest V+ setting.
    vminus_min_V  -- lowest (most negative) V- setting.
    """

    vplus_max_V: float = 5.0
    vminus_min_V: float = -5.0


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
    sim: Sim = None
    hardware: Hardware = None
    supplies: Supplies = None
    ui: UI = None

    def __post_init__(self):
        self.channel_1 = self.channel_1 or Channel()
        self.channel_2 = self.channel_2 or Channel()
        self.timebase = self.timebase or Timebase()
        self.trigger = self.trigger or Trigger()
        self.acquisition = self.acquisition or Acquisition()
        self.filter = self.filter or Filter()
        self.sim = self.sim or Sim()
        self.hardware = self.hardware or Hardware()
        self.supplies = self.supplies or Supplies()
        self.ui = self.ui or UI()

    def channel(self, ch: str) -> Channel:
        return self.channel_1 if ch == "ch1" else self.channel_2

    _GROUPS = {"channel_1": Channel, "channel_2": Channel, "timebase": Timebase,
               "trigger": Trigger, "acquisition": Acquisition, "filter": Filter,
               "sim": Sim, "hardware": Hardware, "supplies": Supplies, "ui": UI}

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
