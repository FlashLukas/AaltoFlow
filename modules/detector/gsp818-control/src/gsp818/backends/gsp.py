"""The real GW Instek GSP-818, over VISA (pyvisa). THE ONLY FILE THAT TOUCHES
THE INSTRUMENT'S LIBRARY -- and it imports pyvisa inside `open()`, so the
package still imports on a PC with no VISA installed.

NEVER RUN ON THE INSTRUMENT YET. Sources used (checked 2026-09-27):
  * GSP-818 Programming Manual (GW Instek, 104 pages; read on manualslib,
    "GW Instek GSP-818 Series Programming Manual"): the command syntax below,
    page numbers in the comments ("PM p.NN").
  * GSP-818 User Manual (GW Instek download), Specifications: USB device =
    "USB TMC", LAN 10/100Base; RBW 10 Hz - 3 MHz, VBW 10 Hz - 3 MHz, reference
    level -80 ... +30 dBm, attenuation 0 - 40 dB in 1 dB steps, sweep time
    10 ms - 3000 s, tracking generator 100 kHz - 1.8 GHz at -30 ... 0 dBm;
    detector "Auto" = Normal above 1 MHz span, Pos Peak at or below.
Every line that the manuals do not settle beyond doubt is marked # VERIFY.

Interface: USBTMC (the data sheet). On Windows a USBTMC device needs a VISA
library with a USB driver -- NI-VISA or Keysight IO Libraries -- so the default
is the installed VISA. pyvisa-py can do USBTMC only with pyusb + libusb, which is
not set up by this module. Over LAN the manual documents the IP settings but
not the protocol or port (# VERIFY: try VXI-11 "TCPIP0::<ip>::inst0::INSTR").

How a trace is read. The manual documents :INITiate:CONTinuous ON|OFF but NO
"start one sweep now" command (no :INIT:IMM, no *OPC? behaviour described).
So there are two modes (`hardware.sweep_mode`):
  "wait"    (default) the instrument sweeps continuously. After a trigger the
            brain waits `settle_sweeps` sweep times (+ margin) and then reads
            TRACE1: the sweep that was running at the trigger is not trusted,
            the one after it is whole. Only documented commands.
  "single"  :INIT:CONT OFF once, then :INIT:IMM for every sweep. The usual
            SCPI way, faster (one sweep per trace), but :INIT:IMM is NOT in the
            GSP-818 manual -- # VERIFY before switching to it.

Trace format (PM p.100): `:TRAC? TRACE1` returns comma-separated ASCII numbers
in the active amplitude unit. The manual's example reply starts with a ">" --
stripped defensively.

START-UP READS, NEVER WRITES (Lukas's rule, 2026-09-27). `open()` and
`read_state()` only QUERY: the unit, the span, RBW, ..., the tracking
generator. Nothing is switched off, no unit is changed. Consequences:
  * The amplitude unit stays what the front panel chose (dBm, dBmV, dBuV, V,
    W). It is READ, and traces and the reference level are converted to/from
    dBm HERE, in software (50 ohm input), instead of sending :UNIT:POW DBM.
  * The tracking generator keeps its state; the brain adopts it (TG ON is
    shown as ON). It is still switched off on SHUTDOWN (`close`), unchanged.
  * A trace mode other than Clear/Write, the instrument's own averaging, or
    single-sweep mode would stop a read from being a fresh sweep. They are
    reported at start and corrected only by `ensure_live()`, which the brain
    calls when an ACQUISITION (an explicit request for a fresh trace) begins.
  * Knob values read at start seed the "already applied" cache, so the first
    `configure` sends nothing; a knob that could not be read is assumed
    in sync (`mark_in_sync`) and written only when the user changes it.
"""

from __future__ import annotations

import time

import numpy as np

from ..config import Config
from ..hwlock import HardwareBusy, HardwareLock, claim
from ..model import SweepSettings

GW_INSTEK_USB_VID = "0x2184"      # USB vendor id of Good Will Instrument  # VERIFY in NI MAX
GW_INSTEK_USB_VID_DEC = "8580"    # the same id in decimal: some VISAs print it that way

#: the module's detector names -> the SCPI words (PM p.77)
_DETECTOR_SCPI = {"auto": "AUTO", "normal": "NORM", "pos_peak": "POS",
                  "neg_peak": "NEG", "sample": "SAMP"}


#: the SCPI replies -> the module's detector names (a reply may be long form,
#: e.g. "POSitive"; matched on the first letters)  # VERIFY the reply spelling
_DETECTOR_FROM_SCPI = (("AUTO", "auto"), ("NORM", "normal"), ("POS", "pos_peak"),
                       ("NEG", "neg_peak"), ("SAMP", "sample"))

# Amplitude units (PM p.104) and their relation to dBm on the 50 ohm input.
# P[mW] = V[V]^2 / 50 ohm * 1000, so
#   dBm = 20 log10(V / 1 V) + 30 - 10 log10(50) = dBV + 13.01
#   dBmV = dBV + 60   ->  dBm = dBmV - 46.99
#   dBuV = dBV + 120  ->  dBm = dBuV - 106.99
#   W:  dBm = 10 log10(P / 1 W) + 30
_LOG50 = 10.0 * np.log10(50.0)          # 16.99 dB
_UNITS = ("DBM", "DBMV", "DBUV", "V", "W")
_DB_OFFSET = {"DBM": 0.0,                         # dBm = value - offset
              "DBMV": 60.0 - 30.0 + _LOG50,       # 46.99
              "DBUV": 120.0 - 30.0 + _LOG50}      # 106.99


def _unit_name(reply: str) -> str:
    """':UNIT:POW?' reply -> one of _UNITS ('' if not understood).  # VERIFY spelling"""
    r = reply.strip().upper().replace('"', "")
    return r if r in _UNITS else ""


def _to_dBm(values, unit: str):
    """Numbers in `unit` -> dBm (array or scalar). In a linear unit a value
    <= 0 becomes -inf, which is what it is in dBm."""
    v = np.asarray(values, dtype=float)
    if unit in _DB_OFFSET:
        out = v - _DB_OFFSET[unit]
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            pos = np.where(v > 0, v, np.nan)
            if unit == "V":
                out = 20.0 * np.log10(pos) + 30.0 - _LOG50
            else:                                   # W
                out = 10.0 * np.log10(pos) + 30.0
        out = np.where(np.isnan(out), -np.inf, out)
    return float(out) if np.ndim(out) == 0 else out


def _from_dBm(dbm: float, unit: str) -> float:
    """dBm -> the number to send in `unit` (the inverse of _to_dBm)."""
    if unit in _DB_OFFSET:
        return float(dbm) + _DB_OFFSET[unit]
    if unit == "V":
        return float(10.0 ** ((float(dbm) - 30.0 + _LOG50) / 20.0))
    return float(10.0 ** ((float(dbm) - 30.0) / 10.0))      # W


def _fmt_level(dbm: float, unit: str) -> str:
    """The text sent for a level in the instrument's unit: 0.01 dB in the log
    units, 6 significant digits in the linear ones (0.00 W would be nonsense)."""
    v = _from_dBm(dbm, unit)
    return f"{v:.2f}" if unit in _DB_OFFSET else f"{v:.6g}"


def _on(reply: str) -> bool:
    """'1' / 'ON' -> True; '0' / 'OFF' -> False. Anything else raises."""
    r = reply.strip().upper()
    if r in ("1", "ON"):
        return True
    if r in ("0", "OFF"):
        return False
    raise ValueError(f"not an on/off reply: {reply!r}")


def _num(reply: str) -> float:
    """First number of a reply ('20.000', '3000000 Hz' -> 20.0, 3e6)."""
    return float(reply.strip().lstrip(">").split()[0].split(",")[0])


def _parse_trace(text: str) -> np.ndarray:
    """'-64.73,-68.16,...' -> float array. Tolerates a leading '>' or '#'
    (the manual's example shows one) and a trailing terminator."""
    body = text.strip().lstrip(">#").strip()
    vals = [v for v in body.replace(";", ",").split(",") if v.strip()]
    return np.array([float(v) for v in vals], dtype=float)


def _settings_dict(s: SweepSettings) -> dict:
    return {k: getattr(s, k) for k in (
        "start_Hz", "stop_Hz", "points", "rbw_Hz", "rbw_auto", "vbw_Hz", "vbw_auto",
        "ref_level_dBm", "atten_dB", "atten_auto", "sweep_time_s", "sweep_time_auto",
        "detector", "preamp", "tg_on", "tg_level_dBm")}


class GspAnalyzer:
    simulated = False

    def __init__(self, cfg: Config, resource=None, sleep=time.sleep):
        """`resource` may be an already-open pyvisa-like object (the tests pass
        a fake instrument); None opens `cfg.hardware.resource` in `open()`."""
        self.cfg = cfg
        self._inst = resource
        self._own = resource is None
        self._rm = None
        # Hardware claims (hwlock, Lukas's rule: one physical instrument = one
        # service). The analyser's VISA address is claimed BEFORE the first
        # byte goes to it and held until close(), so no second service --
        # another gsp818, or any module pointed at the same USB/LAN address --
        # can send it commands at the same time. An injected `resource`
        # (the tests' fake) claims nothing: we did not open it.
        self._locks: list[HardwareLock] = []
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # SCPI header -> value last sent (send only changes)
        self._single_armed = False
        self._pending = False
        self._points = 0
        self._sweep_rb_s = 0.0            # sweep time the instrument REPORTS it uses
        self._extra_wait_s = 0.0          # one-off wait after a settings change ("wait" mode)
        self._unit = "DBM"                # the instrument's amplitude unit, READ at start
        self._trace_mode = ""             # TRACE1 mode as read ("WRIT", "MAXH", "VIEW", ...)
        self._inst_avg = None             # the instrument's own averaging (None = unknown)
        self._cont = None                 # :INIT:CONT as read (None = unknown)

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect and identify. Nothing that changes the instrument is sent."""
        hw = self.cfg.hardware
        if self._inst is None:
            try:
                import pyvisa                      # lazy: only the real path needs it
            except ImportError as exc:             # pragma: no cover - depends on the PC
                raise RuntimeError("pyvisa is not installed: "
                                   "uv sync --extra gui --extra real") from exc
            self._rm = pyvisa.ResourceManager(hw.visa_library) if hw.visa_library \
                else pyvisa.ResourceManager()
            try:
                if hw.resource:
                    # A configured address: claim it first. HardwareBusy here
                    # means another service owns this analyser.
                    self._claim(hw.resource)
                    name = hw.resource
                else:
                    # Auto-discovery claims each candidate before asking it
                    # *IDN?, and keeps the claim on the one it returns.
                    name = self._find_usb(self._rm)
                self._inst = self._rm.open_resource(name)
            except BaseException:
                self._abandon_open()               # a failed open leaves nothing claimed
                raise
        inst = self._inst
        try:
            try:
                # VISA session settings: OUR side of the cable, not instrument state
                inst.timeout = int(hw.timeout_s * 1000)
                inst.read_termination = "\n"       # PM p.23: LF terminates a message
                inst.write_termination = "\n"
            except Exception:
                pass
            self._idn = self._query("*IDN?").strip()   # PM p.31
        except BaseException:
            # The analyser did not answer: close the session WITHOUT writing
            # anything (start-up never writes) and give the address back, so
            # a retry -- or another service -- can have it.
            self._abandon_open()
            raise
        self._applied = {}
        self._sweep_rb_s = 0.0
        self._extra_wait_s = 0.0
        self._single_armed = False
        self._unit = "DBM"

    def read_state(self) -> dict:
        """What the instrument is set to, from QUERIES only. A query that fails
        or does not parse leaves its key out (the brain then keeps the .ini
        value and says so). Every value read also seeds the "already applied"
        cache, so the brain's first configure writes nothing.

        Returns the brain's keys (start_Hz, ..., tg_on, tg_level_dBm) plus
        "notes": sentences about front-panel states that make a read not a
        fresh sweep (corrected by ensure_live at the first acquisition, not here)."""
        st: dict = {}

        def q(key, cmd, parse):
            try:
                st[key] = parse(self._query(cmd))
            except Exception:
                pass

        # the unit first: the reference level (and every trace) is expressed in it
        u = ""
        try:
            u = _unit_name(self._query(":UNIT:POW?"))       # PM p.104  # VERIFY reply
        except Exception:
            pass
        self._unit = u or "DBM"
        q("start_Hz", ":FREQ:STAR?", _num)                     # PM p.78  # VERIFY reply in Hz
        q("stop_Hz", ":FREQ:STOP?", _num)
        q("points", ":SWE:POIN?", lambda r: int(round(_num(r))))    # PM p.92  # VERIFY
        q("rbw_auto", ":BAND:AUTO?", _on)                      # PM p.69  # VERIFY
        q("rbw_Hz", ":BAND?", _num)                            # VERIFY reply in Hz
        q("vbw_auto", ":BAND:VID:AUTO?", _on)                  # PM p.71  # VERIFY
        q("vbw_Hz", ":BAND:VID?", _num)
        q("ref_level_dBm", ":DISP:WIN:TRAC:Y:RLEV?",           # PM p.55, in the active unit
          lambda r: _to_dBm(_num(r), self._unit))              # VERIFY
        q("atten_auto", ":POW:ATT:AUTO?", _on)                 # PM p.90  # VERIFY
        q("atten_dB", ":POW:ATT?", _num)
        q("preamp", ":POW:GAIN:AUTO?", _on)                    # PM p.91 (see _commands_for)  # VERIFY
        q("sweep_time_auto", ":SWE:TIME:AUTO?", _on)           # PM p.92  # VERIFY
        q("sweep_time_s", ":SWE:TIME?", lambda r: _num(r) * 1e-3)   # reply in ms  # VERIFY
        q("detector", ":DET?", self._detector_from)            # PM p.77  # VERIFY reply
        q("tg_level_dBm", ":SOUR:POW:TRAC?", _num)             # PM p.94  # VERIFY dBm whatever :UNIT is
        q("tg_on", ":OUTP:TRAC?", _on)                         # PM p.65  # VERIFY
        # front-panel states that decide whether a read is a FRESH sweep
        try:
            self._trace_mode = self._query(":TRAC1:MODE?").strip().upper()   # PM p.100  # VERIFY
        except Exception:
            self._trace_mode = ""
        try:
            self._inst_avg = _on(self._query(":AVER?"))       # PM p.69  # VERIFY
        except Exception:
            self._inst_avg = None
        try:
            self._cont = _on(self._query(":INIT:CONT?"))      # PM p.60  # VERIFY
        except Exception:
            self._cont = None

        # seed the cache with exactly the text configure would send for these values
        known = set(st)
        for header, value, needs in self._commands_for(st):
            if needs <= known:
                self._applied[header] = value

        notes = []
        if u == "":
            notes.append("GSP-818 amplitude unit not understood: traces are taken as dBm")
        elif u != "DBM":
            notes.append(f"GSP-818 amplitude unit is {u}: traces are converted to dBm in "
                         "software (50 ohm); the unit on the instrument is left as it is")
        notes.extend(self._live_problems())
        st["notes"] = notes
        return st

    def mark_in_sync(self, s: SweepSettings) -> None:
        """Every setting the instrument did NOT report is taken as already
        applied with the brain's value, so it is written only when the user
        changes it -- never as a side effect of starting."""
        for header, value in self._commands(s):
            self._applied.setdefault(header, value)

    def ensure_live(self) -> list[str]:
        """Make the next read a FRESH sweep. Called by the brain when an
        acquisition begins (an explicit user request), never at start-up.
        Writes only what is wrong, and returns one sentence per change."""
        out = []
        if self._trace_mode and not self._trace_mode.startswith("WRIT"):
            self._write(":TRAC1:MODE WRIT")        # PM p.100
            out.append(f"TRACE1 was in {self._trace_mode} mode (not a fresh sweep): "
                       "switched to Clear/Write for the acquisition")
            self._trace_mode = "WRIT"
        if self._inst_avg:
            self._write(":AVER OFF")               # PM p.69: the brain averages, in linear power
            out.append("the GSP-818's own trace averaging was on: switched off "
                       "(the acquisition averages in the software)")
            self._inst_avg = False
        if self.cfg.hardware.sweep_mode == "single":
            if not self._single_armed:
                self._write(":INIT:CONT OFF")      # PM p.60: single-sweep mode
                self._single_armed = True
                out.append("GSP-818 put in single-sweep mode (hardware.sweep_mode = single)")
        elif self._cont is False:
            self._write(":INIT:CONT ON")           # "wait" mode needs it sweeping
            self._cont = True
            out.append("the GSP-818 was in single-sweep mode: switched to continuous "
                       "sweep for the acquisition")
        return out

    def _live_problems(self) -> list[str]:
        out = []
        if self._trace_mode and not self._trace_mode.startswith("WRIT"):
            out.append(f"GSP-818 TRACE1 is in {self._trace_mode} mode: the display is not "
                       "a fresh sweep; left as it is until an acquisition starts")
        if self._inst_avg:
            out.append("GSP-818 trace averaging is on; left as it is until an "
                       "acquisition starts")
        if self._cont is False and self.cfg.hardware.sweep_mode != "single":
            out.append("GSP-818 is in single-sweep mode (the trace does not refresh); "
                       "left as it is until an acquisition starts")
        return out

    @staticmethod
    def _detector_from(reply: str) -> str:
        r = reply.strip().upper()
        for scpi, name in _DETECTOR_FROM_SCPI:
            if r.startswith(scpi):
                return name
        raise ValueError(f"unknown detector reply {reply!r}")

    def close(self, tg_off: bool = True) -> None:
        inst, self._inst = self._inst, None
        self._pending = False
        if inst is None:
            # Never opened (e.g. HardwareBusy) or already closed: nothing to
            # switch off -- and "TG off" must NOT go to an analyser that
            # another service owns.
            self._release_locks()
            return
        try:
            if tg_off:                             # False = a restart: TG left as it is
                inst.write(":OUTP:TRAC OFF")       # never leave the TG driving a DUT
            if self._single_armed:
                inst.write(":INIT:CONT ON")        # hand the front panel back sweeping
        except Exception:
            pass
        if self._own:
            try:
                inst.close()
            except Exception:
                pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
                self._rm = None
        self._release_locks()          # only after the session is closed

    # ---- hardware claims (hwlock) -------------------------------------------

    def _claim(self, address: str) -> HardwareLock:
        lock = claim(address, "gsp818")
        self._locks.append(lock)
        return lock

    def _release_locks(self) -> None:
        locks, self._locks = self._locks, []
        for lock in locks:
            lock.release()

    def _abandon_open(self) -> None:
        """Undo a half-done open(): close what WE opened, send nothing, and
        release every claim. An injected resource (tests) is left open."""
        inst = self._inst
        if self._own:
            self._inst = None
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
                self._rm = None
        self._release_locks()

    def idn(self) -> str:
        return self._idn

    # ---- settings --------------------------------------------------------------

    def configure(self, s: SweepSettings) -> dict:
        """Send only what changed, then read back what the instrument uses."""
        sent0 = self._n_sent
        for header, value in self._commands(s):
            self._set(header, value)
        self._points = int(s.points)
        old_sweep_s = self._sweep_rb_s
        rb = self._readback(s)
        self._sweep_rb_s = float(rb.get("sweep_time_s", s.sweep_time_s))
        if self._n_sent != sent0:
            # Something changed. The manual does not say whether a settings
            # change restarts the running sweep; if it does NOT, the sweep in
            # progress (at the OLD sweep time -- possibly much longer, e.g.
            # after a narrow RBW) is part old, part new. Wait it out once more
            # before trusting whole sweeps.  # VERIFY: does a change restart it?
            self._extra_wait_s = old_sweep_s
        return rb

    def _commands(self, s: SweepSettings) -> list[tuple[str, str]]:
        """(header, value) for every setting in `s`, in the order they are sent."""
        return [(h, v) for h, v, _ in self._commands_for(_settings_dict(s))]

    def _commands_for(self, d: dict) -> list[tuple[str, str, set]]:
        """(header, value, keys it needs) from a dict of the brain's keys. ONE
        place builds the text, so the cache seeded from a READ state and a later
        configure compare equal character for character. A command whose keys
        are missing from `d` is left out."""
        out = []

        def add(header, keys, fn):
            if all(k in d for k in keys):
                out.append((header, fn(), set(keys)))

        add(":FREQ:STAR", ("start_Hz",), lambda: f"{d['start_Hz']:.0f}")      # PM p.78ff
        add(":FREQ:STOP", ("stop_Hz",), lambda: f"{d['stop_Hz']:.0f}")
        add(":SWE:POIN", ("points",), lambda: f"{int(d['points'])}")         # PM p.92  # VERIFY range
        add(":BAND:AUTO", ("rbw_auto",), lambda: "ON" if d["rbw_auto"] else "OFF")   # PM p.69-70
        if d.get("rbw_auto") is False:
            add(":BAND", ("rbw_Hz",), lambda: f"{d['rbw_Hz']:.0f}")          # VERIFY: snaps to 1-3 steps?
        add(":BAND:VID:AUTO", ("vbw_auto",), lambda: "ON" if d["vbw_auto"] else "OFF")  # PM p.71
        if d.get("vbw_auto") is False:
            add(":BAND:VID", ("vbw_Hz",), lambda: f"{d['vbw_Hz']:.0f}")
        # in the instrument's OWN unit (read at start, never changed by us)
        add(":DISP:WIN:TRAC:Y:RLEV", ("ref_level_dBm",),                       # PM p.55
            lambda: _fmt_level(d["ref_level_dBm"], self._unit))
        add(":POW:ATT:AUTO", ("atten_auto",), lambda: "ON" if d["atten_auto"] else "OFF")  # PM p.90-91
        if d.get("atten_auto") is False:
            add(":POW:ATT", ("atten_dB",), lambda: f"{int(round(d['atten_dB']))}")
        # PM p.91 names the preamp command ...:GAIN[:STATe]:AUTO -- an odd name for
        # an on/off switch, but that is what the manual prints.  # VERIFY
        add(":POW:GAIN:AUTO", ("preamp",), lambda: "ON" if d["preamp"] else "OFF")
        add(":SWE:TIME:AUTO", ("sweep_time_auto",),                             # PM p.92
            lambda: "ON" if d["sweep_time_auto"] else "OFF")
        if d.get("sweep_time_auto") is False:
            # "ms" suffix: the manual says the default unit is ns and the query
            # answers in ms -- a unit spelled out cannot be misread.  # VERIFY
            add(":SWE:TIME", ("sweep_time_s",), lambda: f"{d['sweep_time_s'] * 1e3:.3f} ms")
        add(":DET", ("detector",), lambda: _DETECTOR_SCPI.get(d["detector"], "AUTO"))  # PM p.77
        # tracking generator: level first, then the switch (PM p.94, p.65)
        add(":SOUR:POW:TRAC", ("tg_level_dBm",), lambda: f"{d['tg_level_dBm']:.1f}")
        add(":OUTP:TRAC", ("tg_on",), lambda: "ON" if d["tg_on"] else "OFF")
        return out

    def _readback(self, s: SweepSettings) -> dict:
        """What the instrument really uses. A query that fails or does not
        parse falls back to the brain's own number (and is not fatal: the
        trace is what matters)."""
        def q(cmd, scale, fallback):
            try:
                return float(self._query(cmd).strip().split()[0]) * scale
            except Exception:
                return fallback
        return {
            "rbw_Hz": q(":BAND?", 1.0, s.rbw_Hz),                   # VERIFY reply in Hz
            "vbw_Hz": q(":BAND:VID?", 1.0, s.vbw_Hz),               # VERIFY reply in Hz
            "atten_dB": q(":POW:ATT?", 1.0, s.atten_dB),
            "sweep_time_s": q(":SWE:TIME?", 1e-3, s.sweep_time_s),  # PM p.92: reply in ms  # VERIFY
        }

    # ---- sweeping ----------------------------------------------------------------

    def start_sweep(self, s: SweepSettings) -> float:
        hw = self.cfg.hardware
        # The instrument's own sweep time counts, not the brain's estimate: an
        # auto sweep time on the GSP-818 need not follow our formula, and a
        # wait shorter than the real sweep reads a half-old trace.
        t = max(float(s.sweep_time_s), self._sweep_rb_s, 0.0)
        if hw.sweep_mode == "single" and self._single_armed:
            # single-sweep mode is entered by ensure_live (an acquisition),
            # never by the background sweeping that starts with the service
            self._write(":INIT:IMM")               # NOT in the manual  # VERIFY
            wait = t + hw.sweep_margin_s
        else:
            # "wait" mode: the instrument sweeps on its own; nothing is written
            wait = max(1, int(hw.settle_sweeps)) * (t + hw.sweep_margin_s) + self._extra_wait_s
            self._extra_wait_s = 0.0
        self._pending = True
        return wait

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise RuntimeError("finish_sweep without start_sweep")
        self._pending = False
        # in the instrument's unit -> dBm here (the unit is never changed on it)
        y = np.asarray(_to_dBm(_parse_trace(self._query(":TRAC? TRACE1")), self._unit),
                       dtype=float).reshape(-1)          # PM p.100
        if self._points and y.size != self._points:
            # A trace of another length means the instrument does not use the
            # point count we asked for -- the frequency grid would be wrong.  # VERIFY
            raise ValueError(f"trace has {y.size} points, expected {self._points} "
                             "(does the GSP-818 accept this point count?)")
        return y, {}

    def abort_sweep(self) -> None:
        # Nothing to cancel on the instrument: in "wait" mode it keeps sweeping,
        # in "single" mode the next :INIT:IMM restarts it.
        self._pending = False

    # ---- plumbing ------------------------------------------------------------------

    def _set(self, header: str, value: str) -> None:
        if self._applied.get(header) != value:
            self._write(f"{header} {value}")
            self._applied[header] = value

    _n_sent = 0                          # settings writes so far (configure compares)

    def _write(self, cmd: str) -> None:
        if self._inst is None:
            raise RuntimeError("GSP-818 is not open")
        self._inst.write(cmd)
        self._n_sent += 1

    def _query(self, cmd: str) -> str:
        if self._inst is None:
            raise RuntimeError("GSP-818 is not open")
        return self._inst.query(cmd)

    def _find_usb(self, rm) -> str:
        """The first USB instrument of GW Instek whose *IDN? says GSP-818."""
        candidates = [r for r in rm.list_resources("USB?*INSTR")
                      if GW_INSTEK_USB_VID.lower() in r.lower() or f"::{GW_INSTEK_USB_VID_DEC}::" in r]
        busy: list[HardwareBusy] = []
        for name in candidates:
            # Claim BEFORE asking *IDN?: an analyser another service owns must
            # not receive even a query from us. Busy -> skip it.
            try:
                lock = claim(name, "gsp818")
            except HardwareBusy as exc:
                busy.append(exc)
                continue
            keep = False
            try:
                inst = rm.open_resource(name)
                try:
                    inst.timeout = 2000
                    keep = "GSP-818" in inst.query("*IDN?").upper()
                finally:
                    inst.close()
            except Exception:
                keep = False
            if keep:
                self._locks.append(lock)            # held until close()
                return name
            lock.release()                          # not ours to keep
        if busy:
            # The only GSP-818(s) on USB belong to another service: say so,
            # naming the holder, rather than "not found".
            raise busy[0]
        raise RuntimeError("no GSP-818 found on USB (is the VISA USB driver installed? "
                           "set hardware.resource to its VISA address)")
