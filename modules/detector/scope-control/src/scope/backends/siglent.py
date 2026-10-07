"""The real scope: Siglent SDS1000CML+ series (the lab's RS PRO RSDS1102CML+)
over VISA (USB-TMC, or GPIB through Siglent's USB-GPIB adapter).

The ONLY file that touches `pyvisa`, imported LAZILY inside open(), so the
package imports and the simulator runs on a PC with no VISA. On the lab PC:
`uv sync --extra gui --extra real` (gotcha #29) plus NI-VISA.

Command set: Siglent's older "SDS1000 series" programming guide (the CML+ and
CNL+ speak it; the newer SDS1000X command set is different). Every command
that has not answered on our instrument carries `# VERIFY`; README "First run
on the instrument" is the checklist.

    channel    C<n>:TRA ON|OFF   C<n>:VDIV <v>V   C<n>:OFST <v>V
               C<n>:CPL D1M|A1M|GND   C<n>:ATTN <x>
    timebase   TDIV <s>S   TRDL <s>S   SARA? (sample rate)   SANU? C<n> (points)
    trigger    TRSE EDGE,SR,<src>,HT,OFF   <src>:TRLV <v>V   <src>:TRSL POS|NEG
               TRMD AUTO|NORM|SINGLE|STOP        src = C1 | C2 | EX | EX5 | LINE
    new data   INR?  (bit 0 = a new signal acquired since the last INR?)
    waveform   WFSU SP,<sparse>,NP,0,FP,0   then   C<n>:WF? DAT2
               -> "C1:WF DAT2,#9<9-digit length><int8 codes>\\n\\n"
               volts = code * VDIV / 25 - OFST
               time  = +TRDL - span/2 + i * SP / SARA,  span = n_received * SP / SARA
               (NOT SANU / SARA: SANU undercounts the memory, see read_traces)

READ-ONLY START (Lukas, 2026-09-27): open() and read_settings() only ask.
The one write the module makes on its own is WFSU (how the NEXT waveform
TRANSFER is thinned, so a 1.4 Mpts record does not take seconds to move): it
changes no acquisition setting and nothing on the screen.

Replies come with a header ("C1:VDIV 5.00E-01V"); `_num` takes the number out
of whatever surrounds it, so the module does not have to switch the headers
off (CHDR OFF would be a write).

MEASURED ON THE LAB'S RSDS1102CML+ (2026-10-06, firmware 6.01.01.25):
  * the waveform is a BINARY block of int8 codes, and a code of 10 is the byte
    0x0A -- the same byte as the text terminator "\n". Read with the
    terminator on, pyvisa stopped INSIDE the block (23 of 20480 bytes), the rest
    stayed queued, and every later reply was shifted or empty: settings read
    wrong, "no number in ''". The block is now read with the terminator OFF
    (restored after), so the read ends at the end of the message.
  * replies carry SI prefixes and units: "SARA 500.0KSa", "TRDL 0.00us",
    "TDIV 1.00E-03s". `_num` scales by the prefix (the 1000x / 1e6x errors).
  * pyvisa's clear() raises VI_ERROR_SYSTEM_ERROR on this scope; to recover a
    queue left out of step (a crashed client), open() drains it by reading
    at a short timeout until nothing comes.
  * "SANU? C1" answers, a bare "SANU?" times out; TRSE carries the holdoff
    ("EDGE,SR,C1,HT,TI,HV,100NS"), so a new source keeps it.
"""

from __future__ import annotations

import math
import re

import numpy as np

from ..hwlock import claim

_SRC_TO_SCPI = {"ch1": "C1", "ch2": "C2", "ext": "EX", "ext5": "EX5", "line": "LINE"}
_SCPI_TO_SRC = {v: k for k, v in _SRC_TO_SCPI.items()}
_CPL_TO_SCPI = {"dc": "D1M", "ac": "A1M", "gnd": "GND"}
_MODES = {"auto": "AUTO", "normal": "NORM", "single": "SINGLE", "stop": "STOP"}
_NUM = re.compile(r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*([a-zA-Z\u00b5]*)")
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "\u00b5": 1e-6, "m": 1e-3,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_UNITS = ("SA/S", "SA", "S", "V", "HZ", "A", "OHM")


def _num(reply: str, time_unit: bool = False) -> float:
    """The LAST number in a reply, scaled by an SI prefix glued to its unit:

        "C1:VDIV 5.00E-01V" -> 0.5      "SARA 500.0KSa" -> 500000.0
        "TRDL 0.00us"       -> 0.0      "TDIV 1.00E-03s" -> 0.001

    (The last number, because the header itself can hold a digit: "C1:...".)
    `time_unit`: the reply is a time -- there SCPI's "MS" means MILLIseconds,
    not mega (case-insensitive SCPI suffixes), while in "500MSa" M is mega."""
    body = reply.split(" ", 1)[-1] if " " in reply else reply
    found = _NUM.findall(body)
    if not found:
        raise ValueError(f"no number in {reply!r}")
    text, suffix = found[-1]
    value = float(text)
    if suffix:
        for unit in _UNITS:                      # strip the unit, keep the prefix
            if suffix.upper().endswith(unit) and len(suffix) > len(unit):
                prefix = suffix[:len(suffix) - len(unit)]
                if time_unit and prefix in ("M", "m"):
                    return value * 1e-3
                return value * _PREFIX.get(prefix, 1.0)
            if suffix.upper() == unit:
                return value
        if suffix in _PREFIX:                    # a bare prefix ("8.00K")
            return value * _PREFIX[suffix]
    return value


def _word(reply: str) -> str:
    """The value part of a headed reply: "TRMD NORM" -> "NORM"."""
    return reply.strip().split(" ", 1)[-1].strip().upper()


def parse_block(raw: bytes) -> np.ndarray:
    """An IEEE-488.2 definite-length block ("#9000001400" + bytes) -> int8 codes.
    Anything before the '#' is the reply header and is skipped."""
    i = raw.find(b"#")
    if i < 0 or i + 2 > len(raw):
        raise ValueError("no binary block in the waveform reply")
    nd = int(raw[i + 1:i + 2])
    n = int(raw[i + 2:i + 2 + nd])
    start = i + 2 + nd
    data = raw[start:start + n]
    if len(data) < n:
        raise ValueError(f"waveform block cut short: {len(data)} of {n} bytes")
    return np.frombuffer(data, dtype=np.int8).astype(float)


class SiglentSDS:
    simulated = False

    def __init__(self, resource: str, timeout_ms: int = 5000):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._rm = None
        self._inst = None
        self._idn = ""
        self._hwlock = None
        self._sparse = None          # the WFSU thinning last sent
        self._n_full = 0             # points in the scope's record, as last received
        self._n_key = None           # ... valid only for this (SARA, SANU) -- one time/div
        self.last_record: dict = {}  # how the last read went (status "last_record")
        self._desync_s = 0.0         # > 0: a late reply may still arrive; drain it first

    # ---- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        # claim the address FIRST: one instrument, one service (hwlock.py)
        self._hwlock = claim(self._resource, "scope")
        try:
            import pyvisa                                   # lazy: real hw only
            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(self._resource)
            self._inst.timeout = self._timeout_ms
            self._inst.write_termination = "\n"
            self._inst.read_termination = "\n"              # text replies; NOT the block
            self._drain()
            self._idn = self._q("*IDN?")
        except BaseException:
            self._release()
            raise

    def _drain(self, timeout_ms: int = 500, limit: int = 200, binary: bool = False) -> int:
        """Read and throw away whatever an earlier client left in the output
        queue (a reply nobody read, the rest of a cut-off waveform), so the
        first query here gets ITS answer. pyvisa's clear() would be the
        textbook way, but this scope answers it with VI_ERROR_SYSTEM_ERROR.
        Reads only; returns the bytes thrown away. `binary`: read whole
        messages (terminator off) -- a late waveform block is full of 0x0A."""
        old = self._inst.timeout
        self._inst.timeout = timeout_ms
        if binary:
            self._inst.read_termination = None
        dropped = 0
        try:
            for _ in range(limit):
                try:
                    chunk = self._inst.read_raw()
                except Exception:                    # VisaIOError timeout: empty
                    break
                if not chunk:
                    break
                dropped += len(chunk)
        finally:
            self._inst.timeout = old
            self._inst.read_termination = "\n"
        return dropped

    def _read_block(self, record_s: float = 0.0) -> bytes:
        """One binary reply, read with the text terminator OFF: an int8 code
        of 10 is the byte 0x0A, and with "\n" as terminator the read stopped
        inside the block (see the module docstring).

        THE TIMEOUT GROWS WITH THE RECORD (lab PC 2026-10-07): at 0.5 s/div the
        record is ~20 s long and the scope answers WF? only when it is
        complete; the fixed 5 s timeout expired, the reply arrived later into
        a queue nobody was reading, and the scope's USB hung until it was
        power cycled. Now: 1.5 x the record + 5 s, never less than the base
        timeout. A timeout that still happens marks the link out of step
        (`_desync_s`): the next query first drains whatever arrives late."""
        old = self._inst.timeout
        self._inst.timeout = max(self._timeout_ms, int((1.5 * record_s + 5.0) * 1000))
        self._inst.read_termination = None
        try:
            return self._inst.read_raw()
        except Exception:
            self._desync_s = max(5.0, 1.5 * record_s + 5.0)
            raise
        finally:
            self._inst.read_termination = "\n"
            self._inst.timeout = old

    def _release(self) -> None:
        try:
            for obj in (self._inst, self._rm):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
        finally:
            self._inst = self._rm = None
            if self._hwlock is not None:
                self._hwlock.release()
                self._hwlock = None

    def close(self) -> None:
        try:
            if self._inst is not None:
                try:
                    self._inst.control_ren(6)       # Go To Local, this device  # VERIFY
                except Exception:
                    pass
        finally:
            self._release()

    def capabilities(self) -> dict:
        return {"model": self._idn.split(",")[1] if self._idn.count(",") >= 2 else "SDS1000CML+",
                "channels": ["ch1", "ch2"], "ext_trigger": True,
                "generator_channels": 0, "max_points": 2_000_000}

    def idn(self) -> str:
        return self._idn

    # ---- helpers -------------------------------------------------------------------
    def _q(self, cmd: str) -> str:
        self._resync()
        return self._inst.query(cmd).strip()

    def _resync(self) -> None:
        """After a timed-out waveform read: wait for (and throw away) the late
        reply before asking anything else, or every later answer is shifted."""
        if self._desync_s:
            wait_ms = int(self._desync_s * 1000)
            self._desync_s = 0.0
            self._drain(timeout_ms=wait_ms, limit=4, binary=True)

    def _w(self, cmd: str) -> None:
        self._inst.write(cmd)

    # ---- settings --------------------------------------------------------------------
    def read_settings(self) -> dict:
        out = {"channels": {}, "unread": []}

        def get(target, key, fn, name):
            try:
                target[key] = fn()
            except Exception:
                target[key] = None
                out["unread"].append(name)

        for ch, n in (("ch1", 1), ("ch2", 2)):
            c = {}
            get(c, "enabled", lambda n=n: _word(self._q(f"C{n}:TRA?")) == "ON", f"{ch}.enabled")
            get(c, "vdiv_V", lambda n=n: _num(self._q(f"C{n}:VDIV?")), f"{ch}.vdiv_V")
            get(c, "offset_V", lambda n=n: _num(self._q(f"C{n}:OFST?")), f"{ch}.offset_V")
            get(c, "coupling", lambda n=n: {"D1M": "dc", "A1M": "ac", "GND": "gnd",
                                            "D50": "dc", "A50": "ac"}[_word(self._q(f"C{n}:CPL?"))],
                f"{ch}.coupling")                                           # VERIFY codes
            get(c, "probe", lambda n=n: _num(self._q(f"C{n}:ATTN?")), f"{ch}.probe")
            out["channels"][ch] = c
        get(out, "tdiv_s", lambda: _num(self._q("TDIV?"), time_unit=True), "tdiv_s")
        get(out, "delay_s", lambda: _num(self._q("TRDL?"), time_unit=True), "delay_s")
        get(out, "sample_rate_Hz", lambda: _num(self._q("SARA?")), "sample_rate_Hz")
        # points in the record, the scope's word: with SARA it gives the
        # record length at THIS time/div (the brain's record_s)
        get(out, "record_points", lambda: _num(self._q("SANU? C1")), "record_points")  # VERIFY
        trg = {}
        get(trg, "source", self._read_source, "trigger.source")
        src = _SRC_TO_SCPI.get(trg.get("source") or "ch1", "C1")
        get(trg, "level_V", lambda: _num(self._q(f"{src}:TRLV?")), "trigger.level_V")
        get(trg, "slope", lambda: {"POS": "rising", "NEG": "falling"}[
            _word(self._q(f"{src}:TRSL?"))], "trigger.slope")                       # VERIFY
        get(trg, "mode", lambda: {"AUTO": "auto", "NORM": "normal", "SINGLE": "single",
                                  "STOP": "stop"}[_word(self._q("TRMD?"))], "trigger.mode")
        out["trigger"] = trg
        return out

    def _read_source(self) -> str:
        # "TRSE EDGE,SR,C1,HT,OFF": the word after SR is the source
        parts = [p.strip().upper() for p in _word(self._q("TRSE?")).split(",")]
        if "SR" in parts and parts.index("SR") + 1 < len(parts):
            return _SCPI_TO_SRC[parts[parts.index("SR") + 1]]
        raise ValueError(f"cannot read the trigger source from {parts}")      # VERIFY

    def set_channel(self, ch: str, **values) -> None:
        n = 1 if ch == "ch1" else 2
        if "enabled" in values:
            self._w(f"C{n}:TRA {'ON' if values['enabled'] else 'OFF'}")
        if "probe" in values:                     # before V/div: it rescales it
            self._w(f"C{n}:ATTN {values['probe']:g}")                          # VERIFY values
        if "coupling" in values:
            self._w(f"C{n}:CPL {_CPL_TO_SCPI[values['coupling']]}")
        if "vdiv_V" in values:
            self._w(f"C{n}:VDIV {values['vdiv_V']:.4E}V")
        if "offset_V" in values:
            self._w(f"C{n}:OFST {values['offset_V']:.4E}V")

    def set_timebase(self, tdiv_s=None, delay_s=None) -> None:
        if tdiv_s is not None:
            self._w(f"TDIV {tdiv_s:.4E}S")
        if delay_s is not None:
            self._w(f"TRDL {delay_s:.4E}S")                                    # VERIFY

    def set_trigger(self, **values) -> None:
        src = values.get("source")
        if src is not None:
            # TRSE carries the holdoff too ("EDGE,SR,C1,HT,TI,HV,100NS"): change
            # only the word after SR, so a new source keeps the holdoff the
            # scope was set to
            try:
                parts = [p.strip() for p in _word(self._q("TRSE?")).split(",")]
                i = [p.upper() for p in parts].index("SR") + 1
                parts[i] = _SRC_TO_SCPI[src]
                arg = ",".join(parts)
            except (ValueError, IndexError):
                arg = f"EDGE,SR,{_SRC_TO_SCPI[src]},HT,OFF"
            self._w(f"TRSE {arg}")                                              # VERIFY
        cur = src or self._read_source()
        s = _SRC_TO_SCPI[cur]
        if "level_V" in values:
            self._w(f"{s}:TRLV {values['level_V']:.4E}V")
        if "slope" in values:
            self._w(f"{s}:TRSL {'POS' if values['slope'] == 'rising' else 'NEG'}")
        if "mode" in values:
            self._w(f"TRMD {_MODES[values['mode']]}")

    # ---- traces ------------------------------------------------------------------------
    def new_trace_ready(self) -> bool:
        # INR? is read-and-clear: bit 0 = a new signal acquired since last time
        return bool(int(_num(self._q("INR?"))) & 1)                             # VERIFY

    def read_traces(self, channels, max_points):
        """The latest record of `channels`, in volts, and its time axis.

        THE TIME AXIS COMES FROM THE DATA, not from SANU (lab PC, 2026-10-06):
        at 1 ms/div "SANU? C1" said 8000 points, but "C1:WF? DAT2" delivered a
        block of 20480 -- the memory holds more than the screen (14 div) and
        SANU does not count it. Building the axis from SANU made 20480 samples
        span 41 ms but start at -8 ms. Now: n points received, SP apart, at
        SARA -> span = n * SP / SARA, centred on the trigger, shifted by the
        delay.

        MEASURED (lab PC 2026-10-07, AFG square on CH2, trigger EXT rising,
        1 ms/div): with delay 0 the edge sat at +0.058 ms -- centring
        confirmed. With TRDL +2.02 ms the old formula (-TRDL) put the edge at
        -3.97 ms, with -2 ms at +4.09 ms: the edge landed at -2 x delay, so the
        sign was inverted. With +TRDL the edge stays at t = 0 and a POSITIVE
        delay moves the window LATER (more of what follows the trigger)."""
        sara = _num(self._q("SARA?"))
        delay = _num(self._q("TRDL?"), time_unit=True)
        out = {}
        sparse = self._sparse or 1
        n_full = self._n_full
        sanu = 0
        for i, ch in enumerate(channels):
            n = 1 if ch == "ch1" else 2
            if i == 0:
                # how much to thin the TRANSFER (not the acquisition, see the
                # module docstring): from the larger of SANU and the record
                # length actually seen last time (SANU undercounts) -- but only
                # a length seen at the SAME sample rate and SANU, i.e. the same
                # time/div: one from another time/div is a different record
                sanu = int(_num(self._q(f"SANU? C{n}")))                      # VERIFY
                learned = self._n_full if self._n_key == (sara, sanu) else 0
                n_full = max(sanu, learned)
                sparse = max(1, int(math.ceil(n_full / max(1, int(max_points)))))
                if sparse != self._sparse:            # only when it changes
                    self._w(f"WFSU SP,{sparse},NP,0,FP,0")                      # VERIFY
                    self._sparse = sparse
            vdiv = _num(self._q(f"C{n}:VDIV?"))
            ofst = _num(self._q(f"C{n}:OFST?"))
            self._resync()
            self._w(f"C{n}:WF? DAT2")
            # the reply may wait for the record to complete: give it time
            record_s = n_full / sara if sara > 0 else 0.0
            codes = parse_block(self._read_block(record_s))
            out[ch] = codes * vdiv / 25.0 - ofst                                # VERIFY 25/div
        lengths = {k: int(v.size) for k, v in out.items()}
        m = min(lengths.values())
        out = {k: v[:m] for k, v in out.items()}
        self._n_full = m * sparse                     # the record's real length
        self._n_key = (sara, sanu)
        dt = sparse / sara
        span = m * dt
        self.last_record = {"sara": sara, "sanu": sanu, "sparse": sparse,
                            "block_points": lengths, "delay_s": delay}
        t = delay - span / 2.0 + np.arange(m) * dt         # measured: see docstring
        return t, out
