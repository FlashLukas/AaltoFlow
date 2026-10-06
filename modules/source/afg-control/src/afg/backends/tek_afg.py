"""The real Tektronix AFG1062 over USB (USB-TMC), in SCPI via PyVISA.

This is the ONLY file that touches `pyvisa`, and it imports it LAZILY (inside
open(), not at module top) -- so the whole package still imports and the
simulator still runs on a machine with no VISA installed. On the lab PC:
`uv sync --extra gui --extra real` (gotcha #29: name every extra you need),
plus a VISA library (NI-VISA or Tektronix TekVISA) for USB-TMC.

Reference: Tektronix AFG1000 Series Programmer Manual. The AFG1000 command set
is a subset of the AFG3000's, and the commands below are written in their
long-form-compatible short form:

    output        OUTPut<n>[:STATe] ON|OFF            OUTPut<n>:STATe?
    load          OUTPut<n>:IMPedance <ohm>|INFinity  OUTPut<n>:IMPedance?
    waveform      SOURce<n>:FUNCtion:SHAPe <shape>    SOURce<n>:FUNCtion:SHAPe?
    frequency     SOURce<n>:FREQuency:FIXed <Hz>      SOURce<n>:FREQuency:FIXed?
    amplitude     SOURce<n>:VOLTage:LEVel:IMMediate:AMPLitude <v>VPP   (query in VOLTage:UNIT)
    offset        SOURce<n>:VOLTage:LEVel:IMMediate:OFFSet <V>
    phase         SOURce<n>:PHASe:ADJust <phase>      (radians on the AFG3000; see phase_unit)
    align         SOURce1:PHASe:INITiate
    pulse duty    SOURce<n>:PULSe:DCYCle <pct>
    ramp symmetry SOURce<n>:FUNCtion:RAMP:SYMMetry <pct>
    modes (read)  SOURce<n>:BURSt:STATe?  SOURce<n>:FREQuency:MODE?  SOURce<n>:AM|FM|PM|FSKey|PWM:STATe?
    errors        SYSTem:ERRor?  ("0,\"No error\"")

ADOPT, DO NOT RESET (Lukas, 2026-09-27). open() sends no *RST and no
setting: it only reads. The one write is *CLS (empties the status / error
registers; nothing changes at the outputs). Both outputs OFF on close() stay.

MEASURED ON THE LAB'S UNIT (2026-10-06, firmware FV:V1.0.2, read-only
queries with SYST:ERR? after each one). What this file does about it:

  * Some queries the manual lists do not exist on this firmware: the unit
    sends an EMPTY answer and puts -102,"Syntax error" into its error queue
    (FUNC:RAMP:SYMM? in every spelling tried, VOLT:UNIT?, ...). Polled twice a
    second, that was three errors a second in the service log. So every
    OPTIONAL query (_OPTIONAL) is PROBED ONCE in open() -- query, then
    SYST:ERR? -- and one that fails is never sent again.
  * PULS:DCYC? answers ('2.7') but ALSO logs -102: "noisy". Its probe answer
    is used once (adopted at start); it is never polled, because a poll must
    not fill the error queue. After that, the duty shown is the one last SET
    from here, and status says it is not read back.
  * OUTP:IMP? answers b'9.9E+37\\xa6\\xb8\\n': the number, then an Ohm sign in
    the Chinese GB2312/GBK code page, which a strict ASCII decode refuses
    (that refusal made the service show "50 ohm" on a high-Z unit). Replies
    are read as RAW BYTES and decoded tolerantly (_decode); numbers are cut
    out of the text (_number). 9.9E+37 is SCPI's "infinity" = high-Z.
  * The first *IDN? after *CLS comes back empty; the second one is complete.

Every line not yet confirmed against the instrument carries `# VERIFY`. The
lab-PC checklist for them is in README.md, "First run on the instrument".
"""

from __future__ import annotations

import math
import re

from ..hwlock import claim
from ..waveforms import load_factor

#: Tektronix AFG1000 series datasheet, AFG1062 column (2 channels, 60 MHz).
#: Volts are into 50 ohm; envelope() scales them for the load setting.
#: Frequencies per waveform: None = the waveform has no frequency (DC, noise).
AFG1062_ENVELOPE = {
    "freq_Hz": {"sine": (1e-6, 60e6), "square": (1e-6, 25e6),        # VERIFY
                "pulse": (1e-3, 25e6), "ramp": (1e-6, 1e6),          # VERIFY
                "arb": (1e-6, 10e6), "noise": None, "dc": None},     # VERIFY arb
    "amp_Vpp_50ohm": (1e-3, 10.0),          # VERIFY reduced above 20/40 MHz?
    "peak_V_50ohm": 5.0,                    # |offset| + Vpp/2 <= 5 V into 50 ohm
    "duty_pct": (0.1, 99.9),                # VERIFY (pulse width >= 16 ns too)
}

# SCPI shape names <-> ours. The instrument answers the SHORT form ("SIN",
# "SQU", "PULS", "RAMP", "PRN", "DC"); anything else is an arbitrary or
# built-in special waveform (SINC, GAUS, USER1, EMEM, ...) -> "arb".
_SHAPE_SET = {"sine": "SIN", "square": "SQU", "pulse": "PULS", "ramp": "RAMP",
              "noise": "PRN", "dc": "DC"}
_SHAPE_GET = {"SIN": "sine", "SINUSOID": "sine", "SQU": "square", "SQUARE": "square",
              "PULS": "pulse", "PULSE": "pulse", "RAMP": "ramp", "PRN": "noise",
              "PRNOISE": "noise", "DC": "dc"}

#: at or above this an IMPedance reply means INFinity = high-Z. The AFG1062
#: answers 9.9E+37 (SCPI's "infinity"; measured 2026-10-06). A real load
#: setting is at most 10 kohm, so nothing from 1 Mohm up can be one.
_HIGHZ_OHM = 1e6

#: The queries the manual lists but a firmware may lack, probed ONCE in open()
#: on both channels ({n} = 1, 2). Only those found "ok" are ever sent again.
#: Measured on FV:V1.0.2: burst, freq_mode and the five modulation states
#: answer cleanly; duty answers but logs -102 ("noisy"); symmetry and
#: volt_unit do not exist (empty answer + -102).
_OPTIONAL = {
    "volt_unit": "SOUR{n}:VOLT:UNIT?",
    "duty": "SOUR{n}:PULS:DCYC?",
    "symmetry": "SOUR{n}:FUNC:RAMP:SYMM?",
    "burst": "SOUR{n}:BURS:STAT?",
    "freq_mode": "SOUR{n}:FREQ:MODE?",
    "am": "SOUR{n}:AM:STAT?",
    "fm": "SOUR{n}:FM:STAT?",
    "pm": "SOUR{n}:PM:STAT?",
    "fsk": "SOUR{n}:FSK:STAT?",
    "pwm": "SOUR{n}:PWM:STAT?",
}
OK, NOISY, NO = "ok", "noisy", "no"

#: a decimal number at the start of a reply: "9.9E+37" out of "9.9E+37<Ohm>"
_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class NoReply(Exception):
    """The instrument sent an empty answer -- what this firmware does for a
    query it does not know."""


def _decode(raw) -> str:
    """Instrument bytes -> text, never failing.

    The AFG1062 talks ASCII, except that it writes units in a Chinese code
    page (the Ohm sign after an impedance is b'\\xa6\\xb8' in GB2312/GBK).
    ASCII is tried first, then GBK (which turns those two bytes into the Ohm
    sign), then latin-1, which accepts any byte at all. A unit glued to a
    number is dropped by _number(), so its exact spelling does not matter."""
    if isinstance(raw, str):                 # a VISA layer that already decoded
        return raw.strip()
    for codec in ("ascii", "gbk"):
        try:
            return raw.decode(codec).strip()
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1").strip()


def _number(text: str) -> float:
    """The number at the start of a reply, ignoring a unit after it
    ("INF"/"INFINITY" -> inf). NoReply on an empty reply, ValueError on text."""
    t = text.strip()
    if not t:
        raise NoReply("empty reply")
    if t.upper().startswith("INF"):
        return math.inf
    m = _NUM_RE.match(t)
    if not m:
        raise ValueError(f"not a number: {t!r}")
    return float(m.group(0))


def _on(reply: str) -> bool:
    return reply.strip().upper() in ("1", "ON", "+1")


class TekAFG:
    """Drives a physical Tektronix AFG1062. Implements the WaveGen interface.

    phase_unit -- what the instrument's PHASe:ADJust speaks when no unit is
                  given: "rad" (the AFG3000's documented default) or "deg".
                  Settings > Hardware; # VERIFY on the AFG1062 (README).
    """

    def __init__(self, resource: str, timeout_ms: int = 3000,
                 phase_unit: str = "rad"):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._phase_rad = str(phase_unit).lower().startswith("rad")
        self._rm = None
        self._inst = None
        self._idn = ""
        # What the probe in open() found: key of _OPTIONAL -> OK / NOISY / NO,
        # and the probe's answers per (key, channel number). A NOISY query's
        # answer is handed out ONCE, to the first read of that channel (the
        # brain's adoption at start).
        self.support: dict[str, str] = {}
        self._once: dict[tuple, str] = {}
        self._probe_notes: list[str] = []
        # The claim on the VISA address (hwlock.py): held while the instrument
        # is open, so no second service can talk to this generator.
        self._hwlock = None

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        # CLAIM FIRST, before a single byte reaches the bus. If another service
        # holds the address, HardwareBusy leaves here and we touched nothing.
        self._hwlock = claim(self._resource, "afg")
        try:
            import pyvisa                                   # lazy: real hw only
            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(self._resource)
            self._inst.timeout = self._timeout_ms
            self._inst.write_termination = "\n"             # VERIFY (USB-TMC: harmless)
            self._inst.read_termination = "\n"              # VERIFY
            # *CLS empties status + error queue -- nothing the outputs feel --
            # so later SYST:ERR? reports only what WE cause.
            self._inst.write("*CLS")
            # Measured: the FIRST *IDN? after *CLS comes back empty, a second
            # one is complete. So ask again once. Silent (or timed out) twice
            # = nothing we know is there: open() fails, as it always did.
            self._idn = self._q_or_empty("*IDN?")
            if not self._idn:
                self._idn = self._q("*IDN?")
            if not self._idn:
                raise NoReply(f"{self._resource} did not answer *IDN? (twice)")
            self._probe()
        except BaseException:
            # a failed open must not keep the address claimed, and must not
            # leave a half-open session that close() would send OUTP OFF to
            self._release()
            raise

    def _probe(self) -> None:
        """Find out ONCE which optional queries this firmware understands.

        Each one goes out with SYST:ERR? straight after it:
          * an answer and no error             -> OK: polled from now on;
          * an answer AND an error (PULS:DCYC? on FV:V1.0.2) -> NOISY: the
            answer is used once, at adoption; the query is never polled;
          * no answer (empty, or timed out)    -> NO: never sent again.
        A key is OK only if it is OK on both channels. The errors the probe
        causes are drained here, so they never reach the brain's log. Queries
        only -- nothing is set (adopt-on-start rule)."""
        self._drain_quiet()                     # whatever *CLS / *IDN? left behind
        old_timeout = self._inst.timeout
        # An unknown query might time out rather than answer empty; each then
        # costs the whole timeout, once. Keep it short while probing.
        self._inst.timeout = min(self._timeout_ms, 1000)
        try:
            for key, tmpl in _OPTIONAL.items():
                verdicts = []
                for n in (1, 2):
                    reply = self._q_or_empty(tmpl.format(n=n))
                    errs = self._drain_quiet()
                    if not reply:
                        verdicts.append(NO)
                        continue
                    verdicts.append(NOISY if errs else OK)
                    self._once[(key, n)] = reply
                self.support[key] = (NO if NO in verdicts else
                                     NOISY if NOISY in verdicts else OK)
        finally:
            self._inst.timeout = old_timeout
        bad = [f"{_OPTIONAL[k].format(n='<n>')} ({v})"
               for k, v in self.support.items() if v != OK]
        if bad:
            self._probe_notes.append("queries this firmware does not answer cleanly "
                                     "(probed once, never polled): " + ", ".join(bad))
        if self.support.get("volt_unit") != OK:
            self._probe_notes.append("no VOLT:UNIT? on this firmware: amplitude "
                                     "replies are taken as Vpp")       # VERIFY

    def probe_report(self) -> list[str]:
        """What the probe in open() found, as log lines. An OPTIONAL backend
        method: the brain emits these as info events at start if it exists."""
        return list(self._probe_notes)

    def _release(self) -> None:
        """Close the VISA objects and give the address back. Sends nothing."""
        try:
            for obj in (self._inst, self._rm):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
        finally:
            self._inst = None
            self._rm = None
            if self._hwlock is not None:
                self._hwlock.release()
                self._hwlock = None

    def close(self, outputs_off: bool = True) -> None:
        # Outputs OFF only through a session we opened: after a refused claim
        # or a failed open there is none, and nothing is sent. outputs_off=False
        # is a restart (shutdown{keep_outputs}): the outputs stay as they are.
        try:
            if self._inst is not None:
                for n in ((1, 2) if outputs_off else ()):
                    try:
                        self._inst.write(f"OUTP{n}:STAT OFF")
                    except Exception:
                        pass
                try:
                    # hand the front panel back to the operator (Go To Local
                    # for THIS device; pyvisa RENLineOperation.address_gtl = 6)
                    self._inst.control_ren(6)               # VERIFY over USB-TMC
                except Exception:
                    pass
        finally:
            self._release()

    # ---- what it can do --------------------------------------------------

    def capabilities(self) -> dict:
        return {"model": "AFG1062", "channels": 2,
                "waveforms": list(_SHAPE_SET), "phase_align": True,
                "load_settable": True}

    def envelope(self, waveform: str, load_ohm: float | None) -> dict:
        return afg1062_envelope(waveform, load_ohm)

    # ---- reading ---------------------------------------------------------

    def _q(self, cmd: str) -> str:
        """Send a query, return the decoded answer ('' when it was empty).

        write() + read_raw() instead of pyvisa's query(): query() decodes with
        a strict ASCII codec and RAISES on the GBK Ohm sign of OUTP:IMP?."""
        self._inst.write(cmd)
        return _decode(self._inst.read_raw())

    def _q_or_empty(self, cmd: str) -> str:
        """_q, with a timeout counted as an empty answer (open() only)."""
        try:
            return self._q(cmd)
        except Exception:
            return ""

    def _qf(self, cmd: str) -> float:
        return _number(self._q(cmd))

    def _optional(self, key: str, n: int) -> str | None:
        """An optional query's answer, or None = cannot be read here.

        OK -> asked now; NOISY -> the probe's answer, handed out ONCE (then
        None); NO -> None, nothing sent."""
        state = self.support.get(key, OK)
        if state == OK:
            return self._q(_OPTIONAL[key].format(n=n))
        if state == NOISY:
            return self._once.pop((key, n), None)
        return None

    def read_channel(self, ch: int) -> dict:
        n = ch + 1
        out: dict = {"unread": [], "not_read_back": []}

        def get(key, fn):
            try:
                out[key] = fn()
            except Exception:
                out[key] = None
                out["unread"].append(key)

        get("output", lambda: _on(self._q(f"OUTP{n}:STAT?")))
        get("waveform", lambda: _SHAPE_GET.get(self._q(f"SOUR{n}:FUNC:SHAP?").upper(), "arb"))
        get("frequency_Hz", lambda: self._qf(f"SOUR{n}:FREQ:FIX?"))
        get("amplitude_Vpp", lambda: self._read_amplitude(n))
        get("offset_V", lambda: self._qf(f"SOUR{n}:VOLT:LEV:IMM:OFFS?"))
        get("phase_deg", lambda: self._read_phase(n))
        # Duty and symmetry cannot be read on every firmware (see _probe).
        # Then they are LEFT OUT and named in "not_read_back": the brain keeps
        # the value it last set (the config's at start) and does not count
        # them as a failed read -- which would leave the channel unsettled.
        # A NOISY one is named in "not_read_back" too, even on the one read
        # that still carries the probe's answer: it is not FOLLOWED after that.
        for key, probe_key in (("duty_pct", "duty"), ("symmetry_pct", "symmetry")):
            try:
                if self.support.get(probe_key, OK) != OK:
                    out["not_read_back"].append(key)
                reply = self._optional(probe_key, n)
                if reply is not None:
                    out[key] = _number(reply)
            except Exception:
                out[key] = None
                out["unread"].append(key)
        get("load_ohm", lambda: self._read_load(n))
        get("mode", lambda: self._read_mode(n))
        return out

    def _read_amplitude(self, n: int) -> float:
        """Amplitude in Vpp whatever unit the front panel was left in."""
        raw = self._qf(f"SOUR{n}:VOLT:LEV:IMM:AMPL?")
        # No VOLT:UNIT? on FV:V1.0.2 (empty + -102): the reply is then taken
        # as Vpp. Measured: AMPL? said 6.000000e+00 on a channel set to 6 Vpp
        # at high-Z. VERIFY with the AFG's amplitude unit switched to Vrms.
        unit = (self._optional("volt_unit", n) or "VPP").upper()
        if unit.startswith("VRMS"):
            return raw * 2.0 * math.sqrt(2.0)        # exact for a sine only # VERIFY other shapes
        if unit.startswith("DBM"):
            # dBm into 50 ohm, sine: P = Vrms^2 / 50
            vrms = math.sqrt(50.0 * 1e-3 * 10.0 ** (raw / 10.0))
            return vrms * 2.0 * math.sqrt(2.0)
        return raw

    def _read_phase(self, n: int) -> float:
        raw = self._qf(f"SOUR{n}:PHAS:ADJ?")
        return math.degrees(raw) if self._phase_rad else raw               # VERIFY unit

    def _read_load(self, n: int) -> float | None:
        # measured at high-Z: b'9.9E+37\xa6\xb8\n' (infinity + a GBK Ohm sign)
        z = self._qf(f"OUTP{n}:IMP?")
        return None if z >= _HIGHZ_OHM else z                 # VERIFY the 50-ohm reply

    def _read_mode(self, n: int) -> str:
        """Continuous, or one of the modes this module does not drive. A query
        the probe found missing counts as 'off' and is not sent. On FV:V1.0.2
        all of them answered cleanly."""
        def ask(key):
            try:
                return self._optional(key, n) or ""
            except Exception:
                return ""
        if _on(ask("burst")):
            return "burst"
        if ask("freq_mode").upper().startswith("SWE"):   # 'CW' measured; VERIFY sweep reply
            return "sweep"
        for key in ("am", "fm", "pm", "fsk", "pwm"):
            if _on(ask(key)):
                return "modulated"
        return "continuous"

    # ---- writing ---------------------------------------------------------

    def set_output(self, ch: int, on: bool) -> None:
        self._inst.write(f"OUTP{ch + 1}:STAT {'ON' if on else 'OFF'}")

    def set_waveform(self, ch: int, waveform: str) -> None:
        if waveform not in _SHAPE_SET:
            raise ValueError(f"cannot select waveform {waveform!r}")
        self._inst.write(f"SOUR{ch + 1}:FUNC:SHAP {_SHAPE_SET[waveform]}")

    def set_frequency(self, ch: int, hz: float) -> None:
        # 12 significant digits: the AFG resolves 1 uHz (or 12 digits)
        self._inst.write(f"SOUR{ch + 1}:FREQ:FIX {hz:.12g}")

    def set_amplitude(self, ch: int, vpp: float) -> None:
        # an explicit VPP suffix, so the front panel's unit (Vrms, dBm) does
        # not change what the number means
        self._inst.write(f"SOUR{ch + 1}:VOLT:LEV:IMM:AMPL {vpp:.6g}VPP")   # VERIFY suffix

    def set_offset(self, ch: int, volts: float) -> None:
        self._inst.write(f"SOUR{ch + 1}:VOLT:LEV:IMM:OFFS {volts:.6g}")

    def set_phase(self, ch: int, deg: float) -> None:
        v = math.radians(deg) if self._phase_rad else deg
        self._inst.write(f"SOUR{ch + 1}:PHAS:ADJ {v:.8g}")                # VERIFY unit

    def set_duty(self, ch: int, pct: float) -> None:
        # Accepted by FV:V1.0.2? NOT tested (the visit was read-only). Its
        # query logs -102, so the brain cannot read the result back; if the
        # setting is refused, the error appears in the service log.
        self._inst.write(f"SOUR{ch + 1}:PULS:DCYC {pct:.6g}")             # VERIFY

    def set_symmetry(self, ch: int, pct: float) -> None:
        # Every spelling of the QUERY failed on FV:V1.0.2; whether the
        # SETTING is accepted is not tested (a -102 in the log would say no).
        self._inst.write(f"SOUR{ch + 1}:FUNC:RAMP:SYMM {pct:.6g}")        # VERIFY

    def set_load(self, ch: int, load_ohm: float | None) -> None:
        arg = "INF" if load_ohm is None else f"{load_ohm:.6g}"
        self._inst.write(f"OUTP{ch + 1}:IMP {arg}")                        # VERIFY

    def align_phase(self) -> None:
        self._inst.write("SOUR1:PHAS:INIT")                                # VERIFY on AFG1000

    def drain_errors(self) -> list[str]:
        errors = []
        for _ in range(20):                       # guard against a runaway queue
            resp = self._q("SYST:ERR?")
            code = resp.split(",", 1)[0].strip()  # '0,"No error"'      # VERIFY form
            if code.lstrip("+") in ("0", ""):
                break
            errors.append(resp)
        return errors

    def _drain_quiet(self) -> list[str]:
        """drain_errors() that never raises (probing)."""
        try:
            return self.drain_errors()
        except Exception:
            return []

    def idn(self) -> str:
        return self._idn


def afg1062_envelope(waveform: str, load_ohm: float | None) -> dict:
    """The AFG1062's range for `waveform` with the load setting `load_ohm`
    (volts into that load). Shared with the simulator, which pretends to be
    an AFG1062."""
    env = AFG1062_ENVELOPE
    k = load_factor(load_ohm)
    f = env["freq_Hz"].get(waveform)
    amp_lo, amp_hi = env["amp_Vpp_50ohm"]
    return {"freq_min_Hz": f[0] if f else None,
            "freq_max_Hz": f[1] if f else None,
            "amp_min_Vpp": amp_lo * k, "amp_max_Vpp": amp_hi * k,
            "peak_max_V": env["peak_V_50ohm"] * k,
            "duty_min_pct": env["duty_pct"][0], "duty_max_pct": env["duty_pct"][1]}
