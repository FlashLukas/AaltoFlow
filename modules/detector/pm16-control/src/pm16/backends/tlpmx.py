"""The REAL power meter, through Thorlabs' TLPMX driver library.

Why TLPMX and not pyvisa/SCPI (the smb route): out of the box Thorlabs installs
its OWN USB driver for the PM16, and NI-VISA (so pyvisa) cannot see a device on
that driver. TLPMX talks to the meter on either driver. It ships with Thorlabs
Optical Parameter Monitor (OPM), which is already installed on the lab PC.

Why ctypes: TLPMX is a plain C DLL. Python's built-in `ctypes` can call it
directly, so this backend needs NO extra package -- nothing to uncomment in
pyproject.toml.

Every signature below was copied from the vendor header on the lab PC,
    C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Include\\TLPMX.h
and the VISA types from visatype.h next to it. Declaring `argtypes` matters: it
makes ctypes convert and CHECK each argument. Without it a Python float is
passed as an int and the meter is told nonsense without any error.

  ViSession = ViUInt32     ViStatus = ViInt32     ViBoolean = ViUInt16 (!)
  ViRsrc    = char*        ViInt16  = short       ViReal64  = double

This is the ONLY file that touches the vendor library, and it loads it inside
`open()` (or `list_resources()`), so the package imports on any PC.
"""

from __future__ import annotations

import ctypes as C
import os
import sys

from .base import FLAG_NAN, FLAG_OK, FLAG_OVERRANGE, FLAG_UNDERRUN

DEFAULT_DLL = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLPMX_64.dll"

CHANNEL = 1                         # TLPM_DEFAULT_CHANNEL: single-channel meters use 1
BUF = 256                           # TLPM_BUFFER_SIZE
ERR_BUF = 512                       # TLPM_ERR_DESCR_BUFFER_SIZE

ATTR_SET_VAL, ATTR_MIN_VAL, ATTR_MAX_VAL = 0, 1, 2

# Positive status codes are WARNINGS, not errors (VISA convention).
_WARN_OFFSET = 0x3FFC0900
WARN_OVERFLOW = _WARN_OFFSET + 1    # VI_INSTR_WARN_OVERFLOW
WARN_UNDERRUN = _WARN_OFFSET + 2    # VI_INSTR_WARN_UNDERRUN
WARN_NAN = _WARN_OFFSET + 3         # VI_INSTR_WARN_NAN

ViSession, ViStatus, ViBoolean = C.c_uint32, C.c_int32, C.c_uint16
ViInt16, ViUInt16, ViUInt32, ViReal64 = C.c_int16, C.c_uint16, C.c_uint32, C.c_double
P = C.POINTER

# name -> argtypes, straight from TLPMX.h
_SIGNATURES = {
    "TLPMX_findRsrc":            [ViSession, P(ViUInt32)],
    "TLPMX_getRsrcName":         [ViSession, ViUInt32, C.c_char_p],
    "TLPMX_getRsrcInfo":         [ViSession, ViUInt32, C.c_char_p, C.c_char_p, C.c_char_p, P(ViBoolean)],
    "TLPMX_init":                [C.c_char_p, ViBoolean, ViBoolean, P(ViSession)],
    "TLPMX_close":               [ViSession],
    "TLPMX_errorMessage":        [ViSession, ViStatus, C.c_char_p],
    "TLPMX_setTimeoutValue":     [ViSession, ViUInt32],
    "TLPMX_identificationQuery": [ViSession, C.c_char_p, C.c_char_p, C.c_char_p, C.c_char_p],
    "TLPMX_getSensorInfo":       [ViSession, C.c_char_p, C.c_char_p, C.c_char_p,
                                  P(ViInt16), P(ViInt16), P(ViInt16), ViUInt16],
    "TLPMX_setWavelength":       [ViSession, ViReal64, ViUInt16],
    "TLPMX_getWavelength":       [ViSession, ViInt16, P(ViReal64), ViUInt16],
    "TLPMX_setPowerAutoRange":   [ViSession, ViBoolean, ViUInt16],
    "TLPMX_getPowerAutorange":   [ViSession, P(ViBoolean), ViUInt16],
    "TLPMX_setPowerRange":       [ViSession, ViReal64, ViUInt16],
    "TLPMX_getPowerRange":       [ViSession, ViInt16, P(ViReal64), ViUInt16],
    # setAvgCnt / setAvgTime exist in the header but the PM16 refuses both
    # (0xBFFF0067 "does not support this operation", measured 2026-09-15).
    "TLPMX_getAvgTime":          [ViSession, ViInt16, P(ViReal64), ViUInt16],
    "TLPMX_measPower":           [ViSession, P(ViReal64), ViUInt16],
    "TLPMX_startDarkAdjust":     [ViSession, ViUInt16],
    "TLPMX_cancelDarkAdjust":    [ViSession, ViUInt16],
    "TLPMX_getDarkAdjustState":  [ViSession, P(ViInt16), ViUInt16],
    "TLPMX_getDarkOffset":       [ViSession, P(ViReal64), ViUInt16],
}


class TLPMXError(RuntimeError):
    """A negative status from TLPMX, with the library's own description."""


def load_dll(path: str = ""):
    """Load TLPMX_64.dll and declare every function we use. Raises a helpful
    error if it is missing, since that is the first thing to go wrong on a new PC."""
    if sys.platform != "win32":
        raise TLPMXError("TLPMX is a Windows library; use the simulator on this OS")
    path = path or DEFAULT_DLL
    if not os.path.isfile(path):
        raise TLPMXError(
            f"TLPMX library not found at {path}. Install Thorlabs Optical Parameter "
            f"Monitor (OPM), or set hardware.dll_path in the config.")
    # The DLL depends on visa64.dll, which lives in System32 with NI-VISA -- on
    # the default search path. Adding the Bin folder covers any sibling DLLs.
    os.add_dll_directory(os.path.dirname(path))
    dll = C.CDLL(path)
    for name, argtypes in _SIGNATURES.items():
        fn = getattr(dll, name)
        fn.argtypes = argtypes
        fn.restype = ViStatus
    return dll


def list_resources(dll_path: str = "") -> list[dict]:
    """Every power meter TLPMX can see: [{index, resource, model, serial,
    manufacturer, available}]. Needs no open session (vi = 0)."""
    dll = load_dll(dll_path)
    count = ViUInt32(0)
    _check(dll, 0, dll.TLPMX_findRsrc(0, C.byref(count)))
    out = []
    for i in range(count.value):
        name = C.create_string_buffer(BUF)
        _check(dll, 0, dll.TLPMX_getRsrcName(0, i, name))
        model, serial, manuf = (C.create_string_buffer(BUF) for _ in range(3))
        avail = ViBoolean(0)
        _check(dll, 0, dll.TLPMX_getRsrcInfo(0, i, model, serial, manuf, C.byref(avail)))
        out.append({"index": i, "resource": name.value.decode(errors="replace"),
                    "model": model.value.decode(errors="replace"),
                    "serial": serial.value.decode(errors="replace"),
                    "manufacturer": manuf.value.decode(errors="replace"),
                    "available": bool(avail.value)})
    return out


def _check(dll, vi: int, status: int) -> int:
    """Raise on an error status; return warnings (positive codes) to the caller."""
    if status < 0:
        buf = C.create_string_buffer(ERR_BUF)
        try:
            dll.TLPMX_errorMessage(vi, status, buf)
            text = buf.value.decode(errors="replace")
        except Exception:
            text = ""
        raise TLPMXError(f"TLPMX error 0x{status & 0xFFFFFFFF:08X}: {text or 'unknown'}")
    return status


def warning_flag(status: int) -> str:
    """Map a measurement warning code to the backend flag vocabulary."""
    return {WARN_OVERFLOW: FLAG_OVERRANGE, WARN_UNDERRUN: FLAG_UNDERRUN,
            WARN_NAN: FLAG_NAN}.get(status, FLAG_OK)


class TLPMXPowerMeter:
    def __init__(self, resource: str = "", dll_path: str = "", timeout_ms: int = 5000):
        self.resource = resource
        self.dll_path = dll_path
        self.timeout_ms = int(timeout_ms)
        self._dll = None
        self._vi = ViSession(0)
        self._idn = ""
        self._sensor = ""

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._dll = load_dll(self.dll_path)
        if self.resource:
            candidates = [self.resource]
        else:
            found = list_resources(self.dll_path)
            if not found:
                raise TLPMXError("no Thorlabs power meter found. Is the PM16 plugged in?")
            # Do NOT filter on the `available` flag. Measured 2026-09-15: in a
            # process that has created a ZeroMQ context (every service does),
            # TLPMX reports the free PM16 as unavailable, yet TLPMX_init opens
            # it fine. Trying to open is the only honest availability test.
            candidates = [r["resource"] for r in found]
        errors = []
        for resource in candidates:
            try:
                # IDQuery on, reset OFF: a reset would throw away the wavelength
                # and range someone set on the meter before we connected.
                self._call("TLPMX_init", resource.encode(), 1, 0, C.byref(self._vi))
            except TLPMXError as exc:
                errors.append(f"{resource}: {exc}")
                continue
            self.resource = resource
            break
        else:
            raise TLPMXError("could not open a power meter (in use by Thorlabs OPM or "
                             "another program?) -- " + "; ".join(errors))
        self._call("TLPMX_setTimeoutValue", self._vi, self.timeout_ms)
        self._idn = self._read_idn()
        self._sensor = self._read_sensor()

    def close(self) -> None:
        if self._dll is not None and self._vi.value:
            try:
                self._dll.TLPMX_close(self._vi)
            finally:
                self._vi = ViSession(0)

    def idn(self) -> str:
        return self._idn

    def sensor_name(self) -> str:
        return self._sensor

    # ---- wavelength ------------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        self._call("TLPMX_setWavelength", self._vi, float(nm), CHANNEL)

    def get_wavelength(self) -> float:
        return self._get_real("TLPMX_getWavelength", ATTR_SET_VAL)

    def wavelength_range(self) -> tuple[float, float]:
        return (self._get_real("TLPMX_getWavelength", ATTR_MIN_VAL),
                self._get_real("TLPMX_getWavelength", ATTR_MAX_VAL))

    # ---- range -----------------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        self._call("TLPMX_setPowerAutoRange", self._vi, 1 if on else 0, CHANNEL)

    def get_auto_range(self) -> bool:
        v = ViBoolean(0)
        self._call("TLPMX_getPowerAutorange", self._vi, C.byref(v), CHANNEL)
        return bool(v.value)

    def set_range(self, watts: float) -> None:
        # Measured on the PM16-121: it snaps UP to its next range, which are
        # 100x apart (0.174 mW, 17.4 mW, 1.74 W) -- asking for 0.52 mW gave
        # 17.4 mW. It does NOT switch auto-range off by itself; the brain does.
        self._call("TLPMX_setPowerRange", self._vi, float(watts), CHANNEL)

    def get_range(self) -> float:
        return self._get_real("TLPMX_getPowerRange", ATTR_SET_VAL)

    def range_limits(self) -> tuple[float, float]:
        return (self._get_real("TLPMX_getPowerRange", ATTR_MIN_VAL),
                self._get_real("TLPMX_getPowerRange", ATTR_MAX_VAL))

    # ---- averaging -------------------------------------------------------
    def average_time_s(self) -> float:
        return self._get_real("TLPMX_getAvgTime", ATTR_SET_VAL)

    # ---- measurement -----------------------------------------------------
    def measure_power(self) -> tuple[float, str]:
        # Blocks ~58 ms on the PM16-121 and returns a NEW value every call
        # (measured), so a reading started after a trigger is a fresh one.
        v = ViReal64(0.0)
        status = self._call("TLPMX_measPower", self._vi, C.byref(v), CHANNEL)
        # VERIFY: which warning the PM16 raises when saturated (not provoked yet).
        return float(v.value), warning_flag(status)

    # ---- zero ------------------------------------------------------------
    def start_zero(self) -> None:
        self._call("TLPMX_startDarkAdjust", self._vi, CHANNEL)

    def cancel_zero(self) -> None:
        self._call("TLPMX_cancelDarkAdjust", self._vi, CHANNEL)

    def zero_running(self) -> bool:
        v = ViInt16(0)
        self._call("TLPMX_getDarkAdjustState", self._vi, C.byref(v), CHANNEL)
        return v.value == 1                       # TLPM_STAT_DARK_ADJUST_RUNNING

    def dark_offset(self) -> float:
        v = ViReal64(0.0)
        self._call("TLPMX_getDarkOffset", self._vi, C.byref(v), CHANNEL)
        return float(v.value)

    # ---- internals -------------------------------------------------------
    def _call(self, name: str, *args) -> int:
        if self._dll is None:
            raise TLPMXError("power meter is not open")
        return _check(self._dll, self._vi.value, getattr(self._dll, name)(*args))

    def _get_real(self, name: str, attribute: int) -> float:
        v = ViReal64(0.0)
        self._call(name, self._vi, attribute, C.byref(v), CHANNEL)
        return float(v.value)

    def _read_idn(self) -> str:
        bufs = [C.create_string_buffer(BUF) for _ in range(4)]
        try:
            self._call("TLPMX_identificationQuery", self._vi, *bufs)
        except TLPMXError:
            return ""
        manuf, name, serial, fw = (b.value.decode(errors="replace") for b in bufs)
        return f"{manuf} {name} S/N {serial} fw {fw}".strip()

    def _read_sensor(self) -> str:
        name, snr, msg = (C.create_string_buffer(BUF) for _ in range(3))
        t, st, fl = ViInt16(0), ViInt16(0), ViInt16(0)
        try:
            self._call("TLPMX_getSensorInfo", self._vi, name, snr, msg,
                       C.byref(t), C.byref(st), C.byref(fl), CHANNEL)
        except TLPMXError:
            return ""
        s = name.value.decode(errors="replace")
        n = snr.value.decode(errors="replace")
        return f"{s} ({n})" if n else s
