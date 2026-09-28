"""Configuration: every tunable of the NI USB-6001 module in one place.

Same idea as the other modules -- Python dataclasses with defaults, saved to /
loaded from a plain-text .ini file. What is new here: a DAQ has MANY identical
channels, so three groups hold a LIST of per-channel settings:

    [ai]            samples per read, sample rate (one ADC, one clock: shared)
    [ai.ai0] ...    per analog input: enabled, name, terminal, unit, slope, offset
    [ao.ao0] ...    per analog output: name, min_V, max_V (safety limits)
    [dio.p0.0] ...  per digital line: direction in/out/unused, name, initial, safe_state

Units are explicit in field names (volts _V, hertz _Hz, seconds _s).

WHAT APPLIES WHEN (Lukas, 2026-09-28: "define some digital ports as ins and
some as outs ... reconfigured upon restart"):
  * The LAYOUT -- which AI channels are enabled, their terminal configuration,
    and the direction of every digital line -- is applied ONCE, when the
    service starts, because it decides which DAQmx tasks exist. Change it in
    Settings (or the .ini) and restart the service. There is no live
    direction switch on purpose: a line must never flip from "input" to
    "driven output" in the middle of a measurement.
  * Everything else (names, scales, AO limits, rate, samples) applies at once.

NI USB-6001 facts used below (VERIFY against NI's spec sheet / NI MAX):
  8 AI single-ended or 4 differential (ai0/ai4, ai1/ai5, ai2/ai6, ai3/ai7),
  14 bit, +-10 V only, 20 kS/s AGGREGATE (shared by all enabled channels);
  2 AO (ao0, ao1), +-10 V; 13 DIO: port0 line0..7, port1 line0..3, port2 line0.
"""

from __future__ import annotations

import configparser
import math
from dataclasses import asdict, dataclass, field, fields

# ---- the fixed hardware map of a USB-6001 ------------------------------------

AI_CHANNELS = tuple(f"ai{i}" for i in range(8))
AO_CHANNELS = ("ao0", "ao1")
#: The 13 digital lines, in the order they appear in every per-line list.
#: "p1.2" = port1/line2, the NI MAX spelling "Dev1/port1/line2".
DIO_LINES = tuple([f"p0.{i}" for i in range(8)] + [f"p1.{i}" for i in range(4)] + ["p2.0"])

TERMINALS = ("RSE", "NRSE", "DIFF")
DIRECTIONS = ("in", "out", "unused")
LEVELS = ("leave", "low", "high")


# ---- per-channel settings -------------------------------------------------------

@dataclass
class AIChannel:
    """One analog input. The reading is shown in volts AND, through a straight
    line `scaled = slope * volts + offset`, in the unit you give it -- so a
    Hall probe channel can read mT, a thermometer K. With the defaults (V, 1, 0)
    the scaled value is just the voltage."""

    enabled: bool = False
    name: str = ""
    terminal: str = "RSE"          # RSE | NRSE | DIFF (DIFF only on ai0..ai3, see sanitise)
    unit: str = "V"
    slope: float = 1.0             # unit per volt
    offset: float = 0.0            # unit


@dataclass
class AOChannel:
    """One analog output. min_V/max_V are YOUR safety envelope (e.g. what the
    amplifier behind it tolerates), inside the card's own +-10 V."""

    name: str = ""
    min_V: float = -10.0
    max_V: float = 10.0


@dataclass
class DIOLine:
    """One digital line.

    direction   "in" (read it), "out" (drive it) or "unused" (no task, the
                line is left completely alone). Applied at service start.
    initial     for an OUTPUT line: what to drive when the service starts.
                "leave" (default) writes nothing -- Lukas's adopt-on-start rule.
                "low"/"high" write that level once, right after the task is
                created (the only way to KNOW the level from the start, because
                creating an output task may already drive the line -- VERIFY).
    safe_state  for an OUTPUT line: what to drive on a CLEAN shutdown of the
                service. "leave" (default) keeps the last level; a killed
                process never gets to write it (DAQmx keeps the last level).
    """

    direction: str = "in"
    name: str = ""
    initial: str = "leave"
    safe_state: str = "leave"


def _ai_defaults() -> list:
    # ai0..ai3 on by default: four single-ended inputs is the common case, and
    # an enabled-but-unconnected input only costs a little sample rate.
    return [AIChannel(enabled=i < 4, name=f"AI {i}") for i in range(8)]


def _ao_defaults() -> list:
    return [AOChannel(name=f"AO {i}") for i in range(2)]


def _dio_defaults() -> list:
    # Every line an INPUT by default: an input is high impedance and drives
    # nothing, so a fresh install can never push a level into whatever is
    # wired to the connector. Outputs are something you choose deliberately.
    return [DIOLine(direction="in", name=line.upper()) for line in DIO_LINES]


# ---- the groups -----------------------------------------------------------------

@dataclass
class AI:
    """Settings shared by all analog inputs. The USB-6001 has ONE converter that
    visits the enabled channels in turn, so one sample clock serves them all:
    `rate_Hz` is per channel, and rate x (enabled channels) must stay within
    the card's aggregate 20 kS/s (limits.ai_aggregate_max_Hz)."""

    samples_per_read: int = 100    # one reading = the mean of this many samples
    rate_Hz: float = 1000.0        # per channel -> 100 samples = 0.1 s per reading
    channels: list = field(default_factory=_ai_defaults)


@dataclass
class AO:
    channels: list = field(default_factory=_ao_defaults)


@dataclass
class DIO:
    lines: list = field(default_factory=_dio_defaults)


@dataclass
class Limits:
    """The card's own envelope (VERIFY against the USB-6001 spec sheet). The
    per-channel AO limits are clamped inside these."""

    ao_hw_min_V: float = -10.0
    ao_hw_max_V: float = 10.0
    ai_aggregate_max_Hz: float = 20000.0
    samples_max: int = 10000
    read_time_max_s: float = 1.0   # one averaged reading may take at most this long


@dataclass
class Hardware:
    """Where the card lives and how often it is polled.

    device       the DAQmx device name shown in NI MAX ("Dev1"). The service
                 claims the card by its SERIAL number, read from DAQmx, so two
                 services pointed at one card collide whatever alias NI MAX gave it.
    driver       "sim" or "nidaq"; the --real flag of run_service selects nidaq.
    poll_hz      how often the poll thread reads every AI and DI (the live values).
    """

    device: str = "Dev1"
    driver: str = "sim"
    poll_hz: float = 5.0
    timeout_s: float = 2.0
    # simulator only: ai0/ai1 read back ao0/ao1 (a wire from AO to AI), and the
    # n-th input line reads the n-th output line -- so a scan AO -> AI can be tested.
    sim_ai_loopback: bool = True
    sim_di_loopback: bool = False


@dataclass
class UI:
    theme: str = "dark"            # "dark" or "light", applied at GUI start


# ---- the whole configuration ------------------------------------------------------

#: group name -> (dataclass, name of its per-channel list or None, item class, item ids)
#: EVERY place that walks the groups reads this table (gotcha #4: a group listed
#: in one place and forgotten in another silently does not travel).
_GROUPS = {
    "ai": ("channels", AIChannel, AI_CHANNELS),
    "ao": ("channels", AOChannel, AO_CHANNELS),
    "dio": ("lines", DIOLine, DIO_LINES),
    "limits": (None, None, None),
    "hardware": (None, None, None),
    "ui": (None, None, None),
}
_CLASSES = {"ai": AI, "ao": AO, "dio": DIO, "limits": Limits,
            "hardware": Hardware, "ui": UI}


@dataclass
class Config:
    ai: AI = None
    ao: AO = None
    dio: DIO = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        for name, klass in _CLASSES.items():
            if getattr(self, name) is None:
                setattr(self, name, klass())

    # ---- plain-text persistence --------------------------------------------------

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name, (list_attr, _item, ids) in _GROUPS.items():
            grp = getattr(self, name)
            parser[name] = {f.name: str(getattr(grp, f.name))
                            for f in fields(grp) if f.name != list_attr}
            if list_attr:
                for cid, item in zip(ids, getattr(grp, list_attr)):
                    parser[f"{name}.{cid}"] = {k: str(v) for k, v in asdict(item).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# usb6001-control configuration -- edit values, keep keys.\n"
                     "# [ai.aiN] / [ao.aoN] / [dio.pP.L]: one section per channel / line.\n"
                     "# enabled/terminal/direction apply at the NEXT service start.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        cfg = cls()
        for name, (list_attr, item_cls, ids) in _GROUPS.items():
            grp = getattr(cfg, name)
            if name in parser:
                _fill(grp, parser[name], skip=list_attr)
            if list_attr:
                items = getattr(grp, list_attr)
                for i, cid in enumerate(ids):
                    sec = f"{name}.{cid}"
                    if sec in parser:
                        _fill(items[i], parser[sec])
        sanitise(cfg)
        return cfg


def _fill(obj, section, skip=None) -> None:
    for f in fields(obj):
        if f.name == skip or f.name not in section:
            continue
        # with `from __future__ import annotations`, f.type is a string like
        # "bool"/"float", so every value goes through _cast
        setattr(obj, f.name, _cast(section[f.name], f.type))


def _cast(raw, type_name):
    """Cast a string read from the .ini to the field's declared type.

    The bool case is the classic trap (gotcha #3): bool("False") is True in
    Python, because any non-empty string is "true". So the TEXT is parsed.
    """
    if type_name in ("bool", bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return str(raw)


# ---- validation ------------------------------------------------------------------

def sanitise(cfg: Config) -> list[str]:
    """Bring cfg into a state the card can actually run, IN PLACE.

    Returns one message per fix, so the brain can report them as warnings --
    a silently corrected config is a config nobody understands later.
    """
    msgs: list[str] = []
    lim = cfg.limits

    # -- analog inputs -------------------------------------------------------------
    chans = cfg.ai.channels
    for i, ch in enumerate(chans):
        ch.enabled = _cast(ch.enabled, "bool")
        t = str(ch.terminal).strip().upper()
        if t not in TERMINALS:
            msgs.append(f"ai{i}: unknown terminal {ch.terminal!r}, using RSE")
            t = "RSE"
        if t == "DIFF" and i >= 4:
            # In differential mode ai0..ai3 are the + inputs and ai4..ai7 the
            # matching - inputs, so ai4..ai7 cannot be differential themselves.
            msgs.append(f"ai{i}: DIFF is only possible on ai0..ai3 "
                        f"(ai{i} is the - input of ai{i - 4}); using RSE")
            t = "RSE"
        ch.terminal = t
        for attr in ("slope", "offset"):
            v = float(getattr(ch, attr))
            if not math.isfinite(v):
                msgs.append(f"ai{i}: {attr} {v} is not a number, using "
                            f"{1.0 if attr == 'slope' else 0.0}")
                v = 1.0 if attr == "slope" else 0.0
            setattr(ch, attr, v)
    for i in range(4):
        if chans[i].enabled and chans[i].terminal == "DIFF" and chans[i + 4].enabled:
            chans[i + 4].enabled = False
            msgs.append(f"ai{i + 4} disabled: it is the - input of ai{i}, which is DIFF")

    n = max(1, sum(1 for ch in chans if ch.enabled))
    a = cfg.ai
    a.samples_per_read = int(min(max(1, int(a.samples_per_read)), int(lim.samples_max)))
    rate = float(a.rate_Hz)
    rate_max = float(lim.ai_aggregate_max_Hz) / n
    if not math.isfinite(rate) or rate <= 0:
        msgs.append(f"ai rate {a.rate_Hz} Hz is not usable, using 1000 Hz")
        rate = 1000.0
    if rate > rate_max:
        msgs.append(f"ai rate {rate:g} Hz x {n} channels exceeds the card's "
                    f"{lim.ai_aggregate_max_Hz:g} S/s; using {rate_max:g} Hz")
        rate = rate_max
    a.rate_Hz = rate
    if a.samples_per_read / a.rate_Hz > float(lim.read_time_max_s):
        new = max(1, int(float(lim.read_time_max_s) * a.rate_Hz))
        msgs.append(f"{a.samples_per_read} samples at {a.rate_Hz:g} Hz take longer than "
                    f"{lim.read_time_max_s:g} s; using {new} samples")
        a.samples_per_read = new

    # -- analog outputs --------------------------------------------------------------
    for i, ch in enumerate(cfg.ao.channels):
        lo, hi = float(ch.min_V), float(ch.max_V)
        if lo > hi:
            lo, hi = hi, lo
            msgs.append(f"ao{i}: min > max, swapped")
        clo = min(max(lo, lim.ao_hw_min_V), lim.ao_hw_max_V)
        chi = min(max(hi, lim.ao_hw_min_V), lim.ao_hw_max_V)
        if (clo, chi) != (lo, hi):
            msgs.append(f"ao{i}: limits {lo:g}..{hi:g} V narrowed to the card's "
                        f"{clo:g}..{chi:g} V")
        ch.min_V, ch.max_V = clo, chi

    # -- digital lines -------------------------------------------------------------------
    for line, d in zip(DIO_LINES, cfg.dio.lines):
        v = str(d.direction).strip().lower()
        if v not in DIRECTIONS:
            # "unused" touches nothing -- the only safe guess for a typo.
            msgs.append(f"{line}: unknown direction {d.direction!r}, using 'unused'")
            v = "unused"
        d.direction = v
        for attr in ("initial", "safe_state"):
            v = str(getattr(d, attr)).strip().lower()
            if v not in LEVELS:
                msgs.append(f"{line}: unknown {attr} {getattr(d, attr)!r}, using 'leave'")
                v = "leave"
            setattr(d, attr, v)
    return msgs


# ---- helpers used by brain, describe and GUI ------------------------------------

def line_index(line) -> int:
    """'p0.4', 'P0.4', 'port0/line4', 'Dev1/port0/line4' or 4 -> 4."""
    if isinstance(line, bool):
        raise ValueError(f"not a digital line: {line!r}")
    if isinstance(line, int):
        if 0 <= line < len(DIO_LINES):
            return line
        raise ValueError(f"digital line index {line} out of range 0..{len(DIO_LINES) - 1}")
    s = str(line).strip().lower()
    if s.isdigit():
        return line_index(int(s))
    if "port" in s:
        parts = s.split("/")
        port = [p for p in parts if p.startswith("port")]
        ln = [p for p in parts if p.startswith("line")]
        if port and ln:
            s = f"p{port[-1][4:]}.{ln[-1][4:]}"
    if s in DIO_LINES:
        return DIO_LINES.index(s)
    raise ValueError(f"unknown digital line {line!r} (use p0.0..p0.7, p1.0..p1.3, p2.0)")


def line_id(index: int) -> str:
    """4 -> 'p0_4': the spelling used in describe ids (no dot inside an id)."""
    return DIO_LINES[index].replace(".", "_")


def daqmx_line(device: str, index: int) -> str:
    """4 -> 'Dev1/port0/line4' (the physical channel name DAQmx expects)."""
    port, ln = DIO_LINES[index][1:].split(".")
    return f"{device}/port{port}/line{ln}"
