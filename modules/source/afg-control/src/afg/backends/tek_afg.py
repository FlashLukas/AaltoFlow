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

Every line not yet confirmed against the instrument carries `# VERIFY`. The
lab-PC checklist for them is in README.md, "First run on the instrument".
"""

from __future__ import annotations

import math

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

#: above this an IMPedance reply means INFinity (SCPI answers 9.9E+37)
_HIGHZ_OHM = 1e6


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
            self._idn = self._q("*IDN?")
        except BaseException:
            # a failed open must not keep the address claimed, and must not
            # leave a half-open session that close() would send OUTP OFF to
            self._release()
            raise

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

    def close(self) -> None:
        # Outputs OFF only through a session we opened: after a refused claim
        # or a failed open there is none, and nothing is sent.
        try:
            if self._inst is not None:
                for n in (1, 2):
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
        return self._inst.query(cmd).strip()

    def _qf(self, cmd: str) -> float:
        return float(self._q(cmd))

    def read_channel(self, ch: int) -> dict:
        n = ch + 1
        out: dict = {"unread": []}

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
        get("duty_pct", lambda: self._qf(f"SOUR{n}:PULS:DCYC?"))              # VERIFY
        get("symmetry_pct", lambda: self._qf(f"SOUR{n}:FUNC:RAMP:SYMM?"))     # VERIFY
        get("load_ohm", lambda: self._read_load(n))
        get("mode", lambda: self._read_mode(n))
        return out

    def _read_amplitude(self, n: int) -> float:
        """Amplitude in Vpp whatever unit the front panel was left in."""
        raw = self._qf(f"SOUR{n}:VOLT:LEV:IMM:AMPL?")
        unit = self._q(f"SOUR{n}:VOLT:UNIT?").upper()                      # VERIFY
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
        z = self._qf(f"OUTP{n}:IMP?")                                      # VERIFY
        return None if z >= _HIGHZ_OHM else z

    def _read_mode(self, n: int) -> str:
        """Continuous, or one of the modes this module does not drive. Each
        query is optional: a command the unit lacks counts as 'off'."""
        def flag(cmd):
            try:
                return _on(self._q(cmd))
            except Exception:
                return False
        if flag(f"SOUR{n}:BURS:STAT?"):                                    # VERIFY
            return "burst"
        try:
            if self._q(f"SOUR{n}:FREQ:MODE?").upper().startswith("SWE"):   # VERIFY
                return "sweep"
        except Exception:
            pass
        for mod in ("AM", "FM", "PM", "FSK", "PWM"):                       # VERIFY
            if flag(f"SOUR{n}:{mod}:STAT?"):
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
        self._inst.write(f"SOUR{ch + 1}:PULS:DCYC {pct:.6g}")             # VERIFY

    def set_symmetry(self, ch: int, pct: float) -> None:
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
