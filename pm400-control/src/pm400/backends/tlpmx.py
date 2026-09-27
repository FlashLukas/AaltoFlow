"""The REAL PM400 console, through Thorlabs' TLPMX driver library.

Same library and same calling technique as pm16-control, whose backend is
VERIFIED on a real PM16-121 (2026-09-15). Everything that worked there is kept
unchanged here: loading the DLL, the VISA types, `_check`, opening by TRYING
each resource (suite gotcha #23), IDQuery on / reset off.

Why TLPMX and not pyvisa/SCPI: Thorlabs installs its OWN USB driver for its
meters by default, and NI-VISA (so pyvisa) cannot see a device on that driver.
TLPMX talks to the console on either driver. It ships with Thorlabs Optical
Power Monitor (OPM).

Why ctypes: TLPMX is a plain C DLL. Python's built-in `ctypes` can call it
directly, so this backend needs NO extra package. Declaring `argtypes` matters:
it makes ctypes convert and CHECK each argument; without it a Python float is
passed as an int and the console is told nonsense without any error.

  ViSession = ViUInt32     ViStatus = ViInt32     ViBoolean = ViUInt16 (!)
  ViRsrc    = char*        ViInt16  = short       ViReal64  = double

Sources for what is NEW relative to pm16 (none of it run on a PM400 yet):
  * TLPMX.h on the lab PC (signatures of the pm16 functions, verified there).
  * Thorlabs' TLPMX Python ctypes wrapper (shipped with OPM under
    ...\\TLPMX\\Examples\\Python; a public copy is UCBoulder/Thorlabs-powermeter
    TLPMX.py): setAvgTime/getAvgTime, set/getEnergyRange, measEnergy, measFreq,
    the SENSOR_TYPE_* / TLPM_SENS_FLAG_* constants, and the PID table
    (PM400 = 0x807D, 0x8075 with the firmware-update interface enabled).
    Its notes: energy functions exist on PM100D/PM100USB/PM200/PM400;
    "Energy sensors do not support" dark adjustment; setAccelState (thermopile
    acceleration) is NOT listed for the PM400, so it is not used.
Every call not exercised on a PM400 is marked # VERIFY.

This is the ONLY file that touches the vendor library, and it loads it inside
`open()` (or `list_resources()`), so the package imports on any PC.
"""

from __future__ import annotations

import ctypes as C
import os
import sys

from .base import (FLAG_NAN, FLAG_OK, FLAG_OVERRANGE, FLAG_UNDERRUN, HEAD_NONE,
                   HEAD_OTHER, HEAD_PHOTODIODE, HEAD_PYRO, HEAD_THERMAL,
                   empty_sensor_info)

DEFAULT_DLL = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLPMX_64.dll"

BUF = 256                           # TLPM_BUFFER_SIZE
ERR_BUF = 512                       # TLPM_ERR_DESCR_BUFFER_SIZE

ATTR_SET_VAL, ATTR_MIN_VAL, ATTR_MAX_VAL = 0, 1, 2

# USB product ids (TLPMX wrapper's TLPM_PID_* table). Only used to TELL the
# user which console a resource is; opening never filters on it.
PID_PM400 = 0x807D                  # VERIFY: the PID our unit enumerates with
PID_PM400_DFU = 0x8075              # the same console with the DFU interface on

# getSensorInfo -> type code (TLPMX wrapper, SENSOR_TYPE_*)
SENSOR_TYPE_NONE, SENSOR_TYPE_PD_SINGLE, SENSOR_TYPE_THERMO = 0x0, 0x1, 0x2
SENSOR_TYPE_PYRO, SENSOR_TYPE_4Q = 0x3, 0x4
# ... and the flags word (TLPM_SENS_FLAG_*)
SENS_FLAG_IS_POWER = 0x0001
SENS_FLAG_IS_ENERGY = 0x0002
SENS_FLAG_IS_WAVEL_SET = 0x0020

# Positive status codes are WARNINGS, not errors (VISA convention).
_WARN_OFFSET = 0x3FFC0900
WARN_OVERFLOW = _WARN_OFFSET + 1    # VI_INSTR_WARN_OVERFLOW
WARN_UNDERRUN = _WARN_OFFSET + 2    # VI_INSTR_WARN_UNDERRUN
WARN_NAN = _WARN_OFFSET + 3         # VI_INSTR_WARN_NAN

ViSession, ViStatus, ViBoolean = C.c_uint32, C.c_int32, C.c_uint16
ViInt16, ViUInt16, ViUInt32, ViReal64 = C.c_int16, C.c_uint16, C.c_uint32, C.c_double
P = C.POINTER

# name -> argtypes. The first block is from TLPMX.h (verified with the PM16);
# the second from the TLPMX ctypes wrapper's documented argument types.
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
    "TLPMX_getAvgTime":          [ViSession, ViInt16, P(ViReal64), ViUInt16],
    "TLPMX_measPower":           [ViSession, P(ViReal64), ViUInt16],
    "TLPMX_startDarkAdjust":     [ViSession, ViUInt16],
    "TLPMX_cancelDarkAdjust":    [ViSession, ViUInt16],
    "TLPMX_getDarkAdjustState":  [ViSession, P(ViInt16), ViUInt16],
    "TLPMX_getDarkOffset":       [ViSession, P(ViReal64), ViUInt16],
    # -- new for the PM400 (wrapper-documented; VERIFY against TLPMX.h) --
    "TLPMX_setAvgTime":          [ViSession, ViReal64, ViUInt16],
    "TLPMX_setEnergyRange":      [ViSession, ViReal64, ViUInt16],
    "TLPMX_getEnergyRange":      [ViSession, ViInt16, P(ViReal64), ViUInt16],
    "TLPMX_measEnergy":          [ViSession, P(ViReal64), ViUInt16],
    "TLPMX_measFreq":            [ViSession, P(ViReal64), ViUInt16],
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
            f"TLPMX library not found at {path}. Install Thorlabs Optical Power "
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


def head_kind(sensor_type: int) -> str:
    """getSensorInfo's type code -> this module's head vocabulary."""
    return {SENSOR_TYPE_NONE: HEAD_NONE, SENSOR_TYPE_PD_SINGLE: HEAD_PHOTODIODE,
            SENSOR_TYPE_THERMO: HEAD_THERMAL, SENSOR_TYPE_PYRO: HEAD_PYRO
            }.get(int(sensor_type), HEAD_OTHER)


def decode_sensor(name: str, serial: str, sensor_type: int, flags: int) -> dict:
    """Turn getSensorInfo's raw answer into the dict the brain expects. Pure,
    so it is tested offline."""
    kind = head_kind(sensor_type)
    if kind == HEAD_NONE:
        return empty_sensor_info()
    # The flags say power or energy; fall back on the type if a head sets neither.
    energy = bool(flags & SENS_FLAG_IS_ENERGY) or (
        kind == HEAD_PYRO and not flags & SENS_FLAG_IS_POWER)
    return {"kind": kind, "name": name, "serial": serial, "energy": energy,
            # VERIFY: whether every C-series head sets IS_WAVEL_SET; a head that
            # does not would refuse setWavelength.
            "wavelength_settable": bool(flags & SENS_FLAG_IS_WAVEL_SET) or flags == 0,
            "zero_supported": not energy and kind in (HEAD_PHOTODIODE, HEAD_THERMAL)}


class TLPMXConsole:
    def __init__(self, resource: str = "", dll_path: str = "", timeout_ms: int = 5000,
                 channel: int = 1):
        self.resource = resource
        self.dll_path = dll_path
        self.timeout_ms = int(timeout_ms)
        # The PM400 has ONE sensor connector, so channel 1 (TLPM_DEFAULT_CHANNEL);
        # the argument exists for two-channel consoles such as the PM5020.
        self.channel = int(channel)
        self._dll = None
        self._vi = ViSession(0)
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._dll = load_dll(self.dll_path)
        if self.resource:
            candidates = [self.resource]
        else:
            found = list_resources(self.dll_path)
            if not found:
                raise TLPMXError("no Thorlabs power meter found. Is the PM400 plugged in "
                                 "and switched on?")
            # Do NOT filter on the `available` flag (gotcha #23, measured on the
            # PM16): in a process with a ZeroMQ context TLPMX reports a free
            # meter as unavailable, yet TLPMX_init opens it. Trying to open is
            # the only honest availability test. PM400s are tried first.
            found.sort(key=lambda r: "PM400" not in r["model"].upper())
            candidates = [r["resource"] for r in found]
        errors = []
        for resource in candidates:
            try:
                # IDQuery on, reset OFF: a reset would throw away the wavelength
                # and range someone set on the console before we connected.
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

    def close(self) -> None:
        if self._dll is not None and self._vi.value:
            try:
                self._dll.TLPMX_close(self._vi)
            finally:
                self._vi = ViSession(0)

    def idn(self) -> str:
        return self._idn

    def sensor_info(self) -> dict:
        name, snr, msg = (C.create_string_buffer(BUF) for _ in range(3))
        t, st, fl = ViInt16(0), ViInt16(0), ViInt16(0)
        # VERIFY: that the PM400 reports a head swap here without a re-init.
        self._call("TLPMX_getSensorInfo", self._vi, name, snr, msg,
                   C.byref(t), C.byref(st), C.byref(fl), self.channel)
        return decode_sensor(name.value.decode(errors="replace"),
                             snr.value.decode(errors="replace"),
                             t.value, fl.value & 0xFFFF)

    # ---- wavelength ------------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        self._call("TLPMX_setWavelength", self._vi, float(nm), self.channel)

    def get_wavelength(self) -> float:
        return self._get_real("TLPMX_getWavelength", ATTR_SET_VAL)

    def wavelength_range(self) -> tuple[float, float]:
        # VERIFY: MIN/MAX follow the plugged-in head (they did on the PM16's
        # built-in head; a console could report its own wider range).
        return (self._get_real("TLPMX_getWavelength", ATTR_MIN_VAL),
                self._get_real("TLPMX_getWavelength", ATTR_MAX_VAL))

    # ---- power range -----------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        self._call("TLPMX_setPowerAutoRange", self._vi, 1 if on else 0, self.channel)

    def get_auto_range(self) -> bool:
        v = ViBoolean(0)
        self._call("TLPMX_getPowerAutorange", self._vi, C.byref(v), self.channel)
        return bool(v.value)

    def set_range(self, watts: float) -> None:
        # On the PM16 it snaps UP to the next range and does not switch auto
        # off by itself (the brain does). VERIFY the same on the PM400 heads.
        self._call("TLPMX_setPowerRange", self._vi, float(watts), self.channel)

    def get_range(self) -> float:
        return self._get_real("TLPMX_getPowerRange", ATTR_SET_VAL)

    def range_limits(self) -> tuple[float, float]:
        return (self._get_real("TLPMX_getPowerRange", ATTR_MIN_VAL),
                self._get_real("TLPMX_getPowerRange", ATTR_MAX_VAL))

    # ---- energy range (pyro heads) ---------------------------------------
    def set_energy_range(self, joules: float) -> None:
        self._call("TLPMX_setEnergyRange", self._vi, float(joules), self.channel)  # VERIFY

    def get_energy_range(self) -> float:
        return self._get_real("TLPMX_getEnergyRange", ATTR_SET_VAL)               # VERIFY

    def energy_range_limits(self) -> tuple[float, float]:
        return (self._get_real("TLPMX_getEnergyRange", ATTR_MIN_VAL),             # VERIFY
                self._get_real("TLPMX_getEnergyRange", ATTR_MAX_VAL))

    # ---- averaging -------------------------------------------------------
    def set_avg_time(self, seconds: float) -> None:
        # The console rounds to a multiple of its internal sample period. The
        # USB timeout must stay longer than one reading, so it follows.
        self._call("TLPMX_setAvgTime", self._vi, float(seconds), self.channel)    # VERIFY
        need = int(seconds * 1000) + 2000
        if need > self.timeout_ms:
            self.timeout_ms = need
            self._call("TLPMX_setTimeoutValue", self._vi, self.timeout_ms)

    def get_avg_time(self) -> float:
        return self._get_real("TLPMX_getAvgTime", ATTR_SET_VAL)

    def avg_time_limits(self) -> tuple[float, float]:
        return (self._get_real("TLPMX_getAvgTime", ATTR_MIN_VAL),                 # VERIFY
                self._get_real("TLPMX_getAvgTime", ATTR_MAX_VAL))

    # ---- measurement -----------------------------------------------------
    def measure_power(self) -> tuple[float, str]:
        # On the PM16 each call blocked one averaging time and was always a new
        # value. VERIFY on the PM400 with a thermal head too.
        v = ViReal64(0.0)
        status = self._call("TLPMX_measPower", self._vi, C.byref(v), self.channel)
        # VERIFY: which warning code the console raises when saturated.
        return float(v.value), warning_flag(status)

    def measure_energy(self) -> tuple[float, str]:
        # VERIFY: does measEnergy wait for a NEW pulse, or return the last one
        # again when called faster than the repetition rate? The acquire logic
        # assumes a new pulse per reading.
        v = ViReal64(0.0)
        status = self._call("TLPMX_measEnergy", self._vi, C.byref(v), self.channel)
        return float(v.value), warning_flag(status)

    def measure_frequency(self) -> float:
        v = ViReal64(0.0)
        self._call("TLPMX_measFreq", self._vi, C.byref(v), self.channel)          # VERIFY
        return float(v.value)

    # ---- zero ------------------------------------------------------------
    def start_zero(self) -> None:
        self._call("TLPMX_startDarkAdjust", self._vi, self.channel)  # VERIFY with a thermal head

    def cancel_zero(self) -> None:
        self._call("TLPMX_cancelDarkAdjust", self._vi, self.channel)

    def zero_running(self) -> bool:
        v = ViInt16(0)
        self._call("TLPMX_getDarkAdjustState", self._vi, C.byref(v), self.channel)
        return v.value == 1                       # TLPM_STAT_DARK_ADJUST_RUNNING

    def dark_offset(self) -> float:
        v = ViReal64(0.0)
        self._call("TLPMX_getDarkOffset", self._vi, C.byref(v), self.channel)
        return float(v.value)

    # ---- internals -------------------------------------------------------
    def _call(self, name: str, *args) -> int:
        if self._dll is None:
            raise TLPMXError("power meter is not open")
        return _check(self._dll, self._vi.value, getattr(self._dll, name)(*args))

    def _get_real(self, name: str, attribute: int) -> float:
        v = ViReal64(0.0)
        self._call(name, self._vi, attribute, C.byref(v), self.channel)
        return float(v.value)

    def _read_idn(self) -> str:
        bufs = [C.create_string_buffer(BUF) for _ in range(4)]
        try:
            self._call("TLPMX_identificationQuery", self._vi, *bufs)
        except TLPMXError:
            return ""
        manuf, name, serial, fw = (b.value.decode(errors="replace") for b in bufs)
        return f"{manuf} {name} S/N {serial} fw {fw}".strip()
