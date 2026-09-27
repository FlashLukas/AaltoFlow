"""The real Lake Shore Model 455 DSP gaussmeter, through pyvisa.

THIS IS THE ONLY FILE THAT TALKS TO THE INSTRUMENT. pyvisa is imported lazily
inside open(), so the package imports on a PC without VISA.

Source: "Lake Shore Model 455 DSP Gaussmeter User's Manual", chapter 6 (Remote
Operation): section 6.2 (serial interface: 7 data bits, ODD parity, 1 stop
bit, CR LF terminators, <= 20 messages/s, 50 ms after a command), section
6.1.4.2.2 (operational status bits) and the command reference 6.3
(pages 6-26 .. 6-37). Probe ranges from the DC measurement table (section 1.2).

Interface: not decided yet. Both work through the same VISA calls:
    GPIB    "GPIB0::12::INSTR"   (factory address 12, terminator CR LF)
    RS-232  "ASRL3::INSTR"       (COM3; 9600 baud by default)
Install the `real` extra (pyvisa + pyvisa-py + pyserial). With NI-VISA or
Keysight IO Libraries installed pyvisa uses those; otherwise it falls back to
the pure-Python pyvisa-py, which handles serial but needs a GPIB driver
(linux-gpib / gpib-ctypes) for GPIB.

Everything that has NOT been tried against a real 455 carries `# VERIFY`.
The commands themselves are copied from the manual; what is unverified is
mostly reply FORMATTING and timing (what an overload reading looks like, how
long ZPROBE takes, whether RANGE also switches autorange off).
"""

from __future__ import annotations

import time

from .base import (DC_DIGITS, FLAG_NO_PROBE, FLAG_OK, FLAG_OVERLOAD, MODES,
                   PROBE_RANGES_mT, PROBE_TYPE_CODES, RMS_BANDS, UNIT_CODES,
                   from_mT, to_mT)

#: OPST? bit weights (manual section 6.1.4.2.2)
OPST_NO_PROBE = 1          # bit 0
OPST_OVERLOAD = 2          # bit 1

_CODE_UNITS = {v: k for k, v in UNIT_CODES.items()}
_DIGIT_CODES = {3: 1, 4: 2, 5: 3}                 # RDGMODE <dc resolution>
_CODE_DIGITS = {v: k for k, v in _DIGIT_CODES.items()}
_MODE_CODES = {"dc": 1, "rms": 2}                 # 3 = peak, not offered
_BAND_CODES = {"wide": 1, "narrow": 2}


class LS455Error(RuntimeError):
    pass


def parse_float(reply: str) -> float:
    """'+1.2345E+03' -> 1234.5. Raises ValueError on anything else (e.g. 'OL')."""
    return float(reply.strip())


def range_index_for(ranges: list[float], full_scale_mT: float) -> int:
    """1-based RANGE number: the smallest range >= the request (snap UP)."""
    for i, r in enumerate(ranges):
        if r >= full_scale_mT * 0.999:
            return i + 1
    return len(ranges)


class LakeShore455:
    def __init__(self, resource: str = "GPIB0::12::INSTR", baud_rate: int = 9600,
                 timeout_ms: int = 2000, command_gap_s: float = 0.05,
                 zero_time_s: float = 8.0, resource_manager=None):
        self.resource = resource
        self.baud_rate = int(baud_rate)
        self.timeout_ms = int(timeout_ms)
        self.command_gap_s = float(command_gap_s)
        self.zero_time_s = float(zero_time_s)
        self._rm = resource_manager          # tests inject a fake one
        self._inst = None
        self._unit = "G"
        self._family = ""
        self._zero_t = None
        self._last_write = 0.0

    # ---- low level ---------------------------------------------------------
    def _need(self):
        if self._inst is None:
            raise LS455Error("Lake Shore 455 is not open")
        return self._inst

    def _pace(self):
        # manual 6.2.6: >= 50 ms of silence after a command, <= 20 messages/s
        wait = self._last_write + self.command_gap_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def write(self, cmd: str) -> None:
        inst = self._need()
        self._pace()
        inst.write(cmd)
        self._last_write = time.monotonic()

    def query(self, cmd: str) -> str:
        inst = self._need()
        self._pace()
        reply = inst.query(cmd)
        # the same gap after a query keeps us under 20 messages per second
        self._last_write = time.monotonic()
        return str(reply).strip()

    # ---- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        if self._rm is None:
            try:
                import pyvisa                      # lazy: only the real backend needs it
            except ImportError as exc:
                raise LS455Error("pyvisa is not installed: uv sync --extra gui --extra real") from exc
            try:
                self._rm = pyvisa.ResourceManager()          # NI-VISA / Keysight if present
            except (OSError, ValueError):
                self._rm = pyvisa.ResourceManager("@py")     # pure-Python fallback
        inst = self._rm.open_resource(self.resource)
        inst.timeout = self.timeout_ms
        inst.read_termination = "\r\n"             # manual table 6-6 / IEEE default CR LF
        inst.write_termination = "\r\n"
        if self.resource.upper().startswith("ASRL"):
            # the 455's serial frame is FIXED: 7 data bits, odd parity, 1 stop bit
            from pyvisa import constants           # VERIFY: attribute names on this pyvisa version
            inst.baud_rate = self.baud_rate
            inst.data_bits = 7
            inst.parity = constants.Parity.odd
            inst.stop_bits = constants.StopBits.one
        self._inst = inst
        try:
            self._unit = _CODE_UNITS.get(int(self.query("UNIT?")), "G")     # VERIFY reply '1'
            self._family = self._probe_family()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        inst, self._inst = self._inst, None
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass

    def idn(self) -> str:
        return self.query("*IDN?")                 # 'LSCI,MODEL455,<serial>,<date>'

    def _probe_family(self) -> str:
        try:
            code = int(self.query("TYPE?"))        # VERIFY: 40/41/42 (50..52 user cable)
        except ValueError:
            return ""
        return PROBE_TYPE_CODES.get(code, "")

    def probe_info(self) -> dict:
        info = {"family": self._family, "type_code": -1, "serial": "",
                "sensitivity_mV_per_kG": float("nan")}
        try:
            info["type_code"] = int(self.query("TYPE?"))
            info["serial"] = self.query("PRBSNUM?")                       # VERIFY
            info["sensitivity_mV_per_kG"] = parse_float(self.query("PRBSENS?"))  # VERIFY unit mV/kG
        except (ValueError, LS455Error):
            pass
        return info

    def ranges_mT(self) -> list[float]:
        # An unknown probe gets the HST list, the widest one. VERIFY: TYPE? for
        # the lab's axial probe, and whether it really has five ranges.
        return list(PROBE_RANGES_mT.get(self._family or "HST"))

    # ---- mode ----------------------------------------------------------------
    def set_mode(self, mode: str, dc_digits: int, rms_band: str) -> None:
        if mode not in MODES or int(dc_digits) not in DC_DIGITS or rms_band not in RMS_BANDS:
            raise ValueError(f"bad mode {mode}/{dc_digits}/{rms_band}")
        # keep the peak settings the user may have on the front panel
        cur = self.query("RDGMODE?").split(",")                          # VERIFY 'n,n,n,n,n'
        peak_mode = cur[3].strip() if len(cur) == 5 else "1"
        peak_disp = cur[4].strip() if len(cur) == 5 else "1"
        self.write(f"RDGMODE {_MODE_CODES[mode]},{_DIGIT_CODES[int(dc_digits)]},"
                   f"{_BAND_CODES[rms_band]},{peak_mode},{peak_disp}")

    def get_mode(self) -> tuple[str, int, str]:
        m, d, b = [int(x) for x in self.query("RDGMODE?").split(",")[:3]]
        mode = {1: "dc", 2: "rms", 3: "peak"}.get(m, "dc")               # peak shown as-is
        return mode, _CODE_DIGITS.get(d, 4), "narrow" if b == 2 else "wide"

    # ---- range ----------------------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        self.write(f"AUTO {1 if on else 0}")

    def get_auto_range(self) -> bool:
        return self.query("AUTO?").strip() == "1"

    def set_range(self, full_scale_mT: float) -> None:
        # AUTO 0 first: the manual says a front-panel range choice disables
        # autorange; over the interface we do not rely on it. VERIFY
        self.write("AUTO 0")
        self.write(f"RANGE {range_index_for(self.ranges_mT(), full_scale_mT)}")

    def get_range(self) -> float:
        n = int(self.query("RANGE?"))              # VERIFY: 1-based, lowest first
        r = self.ranges_mT()
        return r[max(1, min(n, len(r))) - 1]

    # ---- units / relative -----------------------------------------------------
    def set_display_unit(self, unit: str) -> None:
        if unit not in UNIT_CODES:
            raise ValueError(f"unknown unit {unit!r}")
        self.write(f"UNIT {UNIT_CODES[unit]}")
        self._unit = unit

    def get_display_unit(self) -> str:
        return self._unit

    def set_relative(self, on: bool, setpoint_mT: float) -> None:
        # RELSP is in the PRESENT display unit (manual p. 6-35)
        self.write(f"RELSP {from_mT(float(setpoint_mT), self._unit):.6E}")   # VERIFY format
        self.write(f"REL {1 if on else 0},1")      # 1 = user-defined setpoint

    # ---- measurement ----------------------------------------------------------
    def read_field(self) -> tuple[float, str]:
        reply = self.query("RDGFIELD?")            # 'nnn.nnnEnn' in present units, DC or RMS
        try:
            value = to_mT(parse_float(reply), self._unit)
        except ValueError:
            value = float("nan")                   # VERIFY: what an overload reply looks like
        # The operational status register says what the number cannot: no probe,
        # or field overload. One extra query per reading. VERIFY bit weights.
        flag = FLAG_OK
        try:
            st = int(self.query("OPST?"))
            if st & OPST_NO_PROBE:
                flag = FLAG_NO_PROBE
            elif st & OPST_OVERLOAD:
                flag = FLAG_OVERLOAD
        except ValueError:
            pass
        if value != value and not flag:            # NaN with no explanation
            flag = FLAG_OVERLOAD
        return value, flag

    # ---- zero -----------------------------------------------------------------
    def start_zero(self) -> None:
        self.write("ZPROBE")                       # needs the zero-gauss chamber
        self._zero_t = time.monotonic()

    def zero_running(self) -> bool:
        # The manual has no "zero finished" query; wait a fixed time. VERIFY
        # how long *CALIBRATING* stays on the display, and whether the meter
        # answers queries meanwhile.
        if self._zero_t is None:
            return False
        if time.monotonic() - self._zero_t >= self.zero_time_s:
            self._zero_t = None
            return False
        return True

    def clear_zero(self) -> None:
        self.write("ZCLEAR")
