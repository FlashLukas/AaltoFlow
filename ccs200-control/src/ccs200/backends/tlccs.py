"""The REAL spectrometer: a Thorlabs CCS200/M through Thorlabs' TLCCS driver.

Why TLCCS and not pyvisa: the CCS series is not a SCPI instrument. It is a USB
device with a vendor protocol (and a firmware that the driver uploads when the
device enumerates), and Thorlabs ships a C library, TLCCS_64.dll, that speaks
it. It is installed with Thorlabs' "ThorSpectra" / CCS driver package, next to
TLPMX (pm16-control) in
    C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Bin\\TLCCS_64.dll

Why ctypes: TLCCS is a plain C DLL. Python's built-in `ctypes` calls it
directly, so this backend needs NO extra package. Same pattern as the
PM16's TLPMX backend (pm16-control/src/pm16/backends/tlpmx.py), which is
verified on hardware.

SOURCES for the signatures and constants below (the header was NOT on the PC
this was written on, so every call is marked # VERIFY until it is compared
with the header installed by the Thorlabs package, TLCCS.h in
...\\VISA\\Win64\\Include):
  * TLCCS.h as mirrored in github.com/xkronosua/microV (hardware/sim/TLCCS.h):
    TLCCS_NUM_PIXELS 3648, CCS200 PID 0x8089, status bits SCAN_IDLE 0x0002,
    SCAN_TRIGGERED 0x0004, SCAN_START_TRANS 0x0008, SCAN_TRANSFER 0x0010,
    WAIT_FOR_EXT_TRIG 0x0080, integration 1e-5 .. 60 s (default 0.01),
    calibration data sets FACTORY 0 / USER 1, and the prototypes of
    tlccs_init / close / setIntegrationTime / getIntegrationTime / startScan /
    getDeviceStatus / getScanData / getWavelengthData / identificationQuery /
    error_message.
  * Thorlabs' "Integrating a CCS Spectrometer in MATLAB" note and the
    ScopeFoundry HW_thorlabs_ccs200 driver: resource string
    "USB0::0x1313::0x8089::M<serial>::RAW", DLL location.

VISA types (visatype.h):
  ViSession = ViUInt32   ViStatus = ViInt32   ViBoolean = ViUInt16 (!)
  ViInt16 = short        ViInt32 = int        ViReal64 = double    ViRsrc = char*

This is the ONLY file that touches the vendor library, and it loads it inside
`open()`, so the package imports on any PC.
"""

from __future__ import annotations

import ctypes as C
import os
import sys
import time

import numpy as np

DEFAULT_DLL = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll"

NUM_PIXELS = 3648                   # TLCCS_NUM_PIXELS
CCS200_PID = 0x8089                 # VERIFY on the unit: Device Manager > Hardware Ids
BUF = 256                           # TLCCS_BUFFER_SIZE
ERR_BUF = 512                       # TLCCS_ERR_DESCR_BUFFER_SIZE

STATUS_SCAN_IDLE = 0x0002           # VERIFY bit values against TLCCS.h
STATUS_SCAN_TRIGGERED = 0x0004
STATUS_SCAN_START_TRANS = 0x0008
STATUS_SCAN_TRANSFER = 0x0010       # "data ready for transfer" -- what we wait for
STATUS_WAIT_FOR_EXT_TRIG = 0x0080

CAL_FACTORY, CAL_USER = 0, 1        # TLCCS_CAL_DATA_SET_FACTORY / _USER

ViSession, ViStatus, ViBoolean = C.c_uint32, C.c_int32, C.c_uint16
ViInt16, ViInt32, ViUInt32, ViReal64 = C.c_int16, C.c_int32, C.c_uint32, C.c_double
P = C.POINTER

# name -> argtypes. # VERIFY every line against the installed TLCCS.h.
_SIGNATURES = {
    "tlccs_init":               [C.c_char_p, ViBoolean, ViBoolean, P(ViSession)],
    "tlccs_close":              [ViSession],
    "tlccs_error_message":      [ViSession, ViStatus, C.c_char_p],
    "tlccs_identificationQuery": [ViSession, C.c_char_p, C.c_char_p, C.c_char_p,
                                  C.c_char_p, C.c_char_p],
    "tlccs_setIntegrationTime": [ViSession, ViReal64],
    "tlccs_getIntegrationTime": [ViSession, P(ViReal64)],
    "tlccs_startScan":          [ViSession],
    "tlccs_getDeviceStatus":    [ViSession, P(ViInt32)],
    "tlccs_getScanData":        [ViSession, P(ViReal64)],
    "tlccs_getWavelengthData":  [ViSession, ViInt16, P(ViReal64), P(ViReal64), P(ViReal64)],
}


class TLCCSError(RuntimeError):
    """A negative status from TLCCS, with the library's own description."""


def load_dll(path: str = ""):
    """Load TLCCS_64.dll and declare every function we use. Raises a helpful
    error if it is missing, since that is the first thing to go wrong on a new PC."""
    if sys.platform != "win32":
        raise TLCCSError("TLCCS is a Windows library; use the simulator on this OS")
    path = path or DEFAULT_DLL
    if not os.path.isfile(path):
        raise TLCCSError(
            f"TLCCS library not found at {path}. Install the Thorlabs CCS driver "
            f"(ThorSpectra), or set hardware.dll_path in the config.")
    # The DLL needs visa64.dll (System32, from NI-VISA or Thorlabs' VISA
    # runtime). Adding its own folder covers any sibling DLLs.
    os.add_dll_directory(os.path.dirname(path))
    dll = C.CDLL(path)
    for name, argtypes in _SIGNATURES.items():
        fn = getattr(dll, name)
        fn.argtypes = argtypes
        fn.restype = ViStatus
    return dll


def find_resources() -> list[str]:
    """Every CCS200 the VISA library can see, as resource strings. Uses the
    standard VISA C API (viOpenDefaultRM / viFindRsrc / viFindNext), because
    TLCCS has no search function of its own. # VERIFY on the lab PC."""
    if sys.platform != "win32":
        return []
    visa = C.WinDLL("visa64.dll")                 # VERIFY: present with any VISA runtime
    rm = ViSession(0)
    if visa.viOpenDefaultRM(C.byref(rm)) < 0:
        return []
    out = []
    try:
        flist, count = ViUInt32(0), ViUInt32(0)
        desc = C.create_string_buffer(BUF)
        expr = f"USB?*::0x1313::0x{CCS200_PID:04X}::?*::RAW".encode()
        if visa.viFindRsrc(rm, expr, C.byref(flist), C.byref(count), desc) >= 0:
            out.append(desc.value.decode(errors="replace"))
            for _ in range(count.value - 1):
                if visa.viFindNext(flist, desc) < 0:
                    break
                out.append(desc.value.decode(errors="replace"))
            visa.viClose(flist)
    finally:
        visa.viClose(rm)
    return out


class TlccsSpectrometer:
    simulated = False

    def __init__(self, resource: str = "", dll_path: str = "", calibration: str = "factory",
                 dll=None, clock=time.monotonic):
        """`dll` lets a test pass a fake library object (tests/fake_tlccs.py);
        normally it is loaded in open()."""
        self.resource = resource
        self.dll_path = dll_path
        self.calibration = calibration
        self._injected = dll
        self._clock = clock
        self._dll = None
        self._vi = ViSession(0)
        self._idn = ""
        self._wl = np.full(NUM_PIXELS, np.nan)
        self._t_set = None                  # integration time last sent (s)
        self._pending = False               # a scan was started and not read
        self._stale = False                 # ... and nobody wants it any more
        self._t_start = 0.0                 # clock() when that scan was started

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._dll = self._injected if self._injected is not None else load_dll(self.dll_path)
        candidates = [self.resource] if self.resource else (
            find_resources() if self._injected is None else [])
        if not candidates:
            raise TLCCSError("no CCS200 found. Is it plugged in, and is the Thorlabs CCS "
                             "driver installed? (or set hardware.resource / --resource)")
        errors = []
        for resource in candidates:
            try:
                # IDQuery on, reset OFF: a reset is not needed to measure and
                # would throw away whatever the instrument was set to. # VERIFY
                self._call("tlccs_init", resource.encode(), 1, 0, C.byref(self._vi))
            except TLCCSError as exc:
                errors.append(f"{resource}: {exc}")
                continue
            self.resource = resource
            break
        else:
            raise TLCCSError("could not open the spectrometer (in use by ThorSpectra or "
                             "another program?) -- " + "; ".join(errors))
        self._idn = self._read_idn()
        self._wl = self._read_wavelengths()
        self._t_set = None
        self._pending = self._stale = False

    def close(self) -> None:
        if self._dll is not None and self._vi.value:
            try:
                self._dll.tlccs_close(self._vi)
            finally:
                self._vi = ViSession(0)
        self._pending = False

    def idn(self) -> str:
        return self._idn

    def wavelengths(self) -> np.ndarray:
        return self._wl.copy()

    # ---- scanning ----------------------------------------------------------
    def start_scan(self, integration_s: float) -> None:
        # Normally the brain has already waited for busy() to go False; this is
        # the backstop for a caller that did not (it blocks at most one exposure).
        deadline = time.monotonic() + (self._t_set or 0.0) + 5.0
        while self.busy() and time.monotonic() < deadline:
            time.sleep(0.002)
        self._pending = self._stale = False
        t = float(integration_s)
        if self._t_set is None or t != self._t_set:
            # VERIFY: whether the first scan after a change is still exposed at
            # the old time (some linear-CCD firmwares need one flush scan).
            self._call("tlccs_setIntegrationTime", self._vi, t)
            self._t_set = t
        self._call("tlccs_startScan", self._vi)          # VERIFY: single scan, software start
        self._pending, self._stale = True, False
        self._t_start = self._clock()

    def scan_ready(self) -> bool:
        if not self._pending:
            return False
        return bool(self._device_status() & STATUS_SCAN_TRANSFER)   # VERIFY the bit

    def read_scan(self) -> np.ndarray:
        if not self._pending:
            raise TLCCSError("read_scan without start_scan")
        buf = (ViReal64 * NUM_PIXELS)()
        # VERIFY: getScanData returns the PROCESSED scan normalised to full
        # scale (0 .. 1.0), which is the unit this module uses throughout.
        self._call("tlccs_getScanData", self._vi, buf)
        self._pending = False
        return np.frombuffer(buf, dtype=np.float64).copy()

    def abort_scan(self) -> None:
        """The CCS has no "cancel this exposure" call (TLCCS.h has none), so an
        abandoned scan is left to finish; busy() reads its data out and throws
        it away before the next start."""
        if self._pending:
            self._stale = True

    def busy(self) -> bool:
        """True while an ABANDONED scan is still exposing.

        Why this matters: if we started the next scan while the old exposure
        was still running, the "data ready" bit would then come from the OLD
        exposure, and its data -- light collected BEFORE the setting change or
        trigger -- would be delivered as the new scan. Nothing would raise.
        So we wait it out: the brain polls this WITHOUT holding its hardware
        lock (a 60 s exposure must not freeze anything), and once the old data
        is ready it is read and discarded here. # VERIFY whether a startScan
        during a running exposure restarts it (then this wait is merely slow)
        or is refused / queued (then it is required)."""
        if not (self._pending and self._stale):
            return False
        if self._device_status() & STATUS_SCAN_TRANSFER:
            buf = (ViReal64 * NUM_PIXELS)()
            self._call("tlccs_getScanData", self._vi, buf)      # discarded
            self._pending = self._stale = False
            return False
        if self._clock() - self._t_start > (self._t_set or 0.0) + 5.0:
            # the old scan never delivered: give up on it rather than hang
            self._pending = self._stale = False
            return False
        return True

    # ---- internals ---------------------------------------------------------

    def _device_status(self) -> int:
        v = ViInt32(0)
        self._call("tlccs_getDeviceStatus", self._vi, C.byref(v))
        return int(v.value)

    def _call(self, name: str, *args) -> int:
        if self._dll is None:
            raise TLCCSError("spectrometer is not open")
        status = getattr(self._dll, name)(*args)
        if status < 0:
            buf = C.create_string_buffer(ERR_BUF)
            try:
                self._dll.tlccs_error_message(self._vi, status, buf)
                text = buf.value.decode(errors="replace")
            except Exception:
                text = ""
            raise TLCCSError(f"{name}: TLCCS error 0x{status & 0xFFFFFFFF:08X}: "
                             f"{text or 'unknown'}")
        return status

    def _read_idn(self) -> str:
        bufs = [C.create_string_buffer(BUF) for _ in range(5)]
        try:
            self._call("tlccs_identificationQuery", self._vi, *bufs)
        except TLCCSError:
            return ""
        manuf, name, serial, fw, drv = (b.value.decode(errors="replace") for b in bufs)
        return f"{manuf} {name} S/N {serial} fw {fw} driver {drv}".strip()

    def _read_wavelengths(self) -> np.ndarray:
        buf = (ViReal64 * NUM_PIXELS)()
        lo, hi = ViReal64(0.0), ViReal64(0.0)
        data_set = CAL_USER if self.calibration == "user" else CAL_FACTORY
        self._call("tlccs_getWavelengthData", self._vi, data_set, buf,
                   C.byref(lo), C.byref(hi))
        return np.frombuffer(buf, dtype=np.float64).copy()
