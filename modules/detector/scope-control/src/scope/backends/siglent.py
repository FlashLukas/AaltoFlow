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
               time  = -TRDL - span/2 + i * SP / SARA,  span = SANU / SARA

READ-ONLY START (Lukas, 2026-09-27): open() and read_settings() only ask.
The one write the module makes on its own is WFSU (how the NEXT waveform
TRANSFER is thinned, so a 1.4 Mpts record does not take seconds to move): it
changes no acquisition setting and nothing on the screen.

Replies come with a header ("C1:VDIV 5.00E-01V"); `_num` takes the number out
of whatever surrounds it, so the module does not have to switch the headers
off (CHDR OFF would be a write).
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
_NUM = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def _num(reply: str) -> float:
    """The LAST number in a reply: "C1:VDIV 5.00E-01V" -> 0.5. (The last,
    because the header itself can hold a digit: "C1:...".)"""
    found = _NUM.findall(reply.split(" ", 1)[-1] if " " in reply else reply)
    if not found:
        raise ValueError(f"no number in {reply!r}")
    return float(found[-1])


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

    # ---- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        # claim the address FIRST: one instrument, one service (hwlock.py)
        self._hwlock = claim(self._resource, "scope")
        try:
            import pyvisa                                   # lazy: real hw only
            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(self._resource)
            self._inst.timeout = self._timeout_ms
            self._inst.write_termination = "\n"             # VERIFY
            self._inst.read_termination = "\n"              # VERIFY (binary: read_raw)
            self._idn = self._q("*IDN?")
        except BaseException:
            self._release()
            raise

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
        return self._inst.query(cmd).strip()

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
        get(out, "tdiv_s", lambda: _num(self._q("TDIV?")), "tdiv_s")
        get(out, "delay_s", lambda: _num(self._q("TRDL?")), "delay_s")             # VERIFY unit
        get(out, "sample_rate_Hz", lambda: _num(self._q("SARA?")), "sample_rate_Hz")
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
            # NB: this also switches the holdoff OFF (it is part of TRSE)
            self._w(f"TRSE EDGE,SR,{_SRC_TO_SCPI[src]},HT,OFF")                # VERIFY
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
        sara = _num(self._q("SARA?"))
        delay = _num(self._q("TRDL?"))
        n_total = None
        out = {}
        sparse = 1
        for ch in channels:
            n = 1 if ch == "ch1" else 2
            if n_total is None:
                n_total = int(_num(self._q(f"SANU? C{n}")))                     # VERIFY
                sparse = max(1, int(math.ceil(n_total / max(1, int(max_points)))))
                # thin the TRANSFER, not the acquisition (see module docstring);
                # only when the factor changes, to keep the bus quiet
                if sparse != self._sparse:
                    self._w(f"WFSU SP,{sparse},NP,0,FP,0")                      # VERIFY
                    self._sparse = sparse
            vdiv = _num(self._q(f"C{n}:VDIV?"))
            ofst = _num(self._q(f"C{n}:OFST?"))
            self._w(f"C{n}:WF? DAT2")
            codes = parse_block(self._inst.read_raw())
            out[ch] = codes * vdiv / 25.0 - ofst                                # VERIFY 25/div
        m = min(v.size for v in out.values())
        out = {k: v[:m] for k, v in out.items()}
        span = n_total / sara
        t = -delay - span / 2.0 + np.arange(m) * sparse / sara                  # VERIFY
        return t, out
