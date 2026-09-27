"""The real Signal Hound SA44B / SA124B (+ USB-TG44A) through sa_api.dll.

THE ONLY FILE THAT TOUCHES THE VENDOR LIBRARY. The DLL is loaded with ctypes
inside `open()`, so the package imports on a PC without the Signal Hound SDK
(no pip dependency: sa_api.dll ships with Signal Hound's Spike / SDK).

Sources used (2026-09-27), every call below is written from them:
  * sa_api.h in the Signal Hound SDK, online reference
    https://signalhound.com/sigdownloads/SDK/online_docs/sa_api/sa__api_8h.html
    (prototypes, the saStatus codes, SA_SWEEPING = 0, SA_TG_SWEEP = 4,
    SA_MIN_MAX = 0, SA_AVERAGE = 1, SA_LOG_SCALE = 0, TG_THRU_0DB = 1,
    saDeviceType: SA44 = 1, SA44B = 2, SA124A = 3, SA124B = 4)
  * "Modes of operation" in the same docs (the order of the configuration
    calls for swept and TG sweep mode) and SA-API-Manual.pdf.
None of it has been run against the instrument yet: every call whose
behaviour could not be confirmed on hardware is marked # VERIFY.

START-UP (Lukas's rule, 2026-09-27: read, do not change). `open()` only
opens the device and asks what it is: saGetDeviceType, saGetSerialNumber,
saGetAPIVersion, and -- the one exception -- saAttachTg + saIsTgAttached.
The analyser keeps no settings of its own (the API holds them in the host
process and has no getter for them), so there is nothing else to adopt, and
nothing is configured, initiated or aborted until the brain is asked to sweep.
saAttachTg PAIRS the TG44A with this handle; it is the only way to learn
whether a TG is there, and it is not documented to switch the TG output
(# VERIFY 3). `hardware.attach_tg = False` skips it.

How a sweep goes: `configure` aborts whatever runs, sends every setting,
`saInitiate`s the mode and asks `saQuerySweepInfo` which bins it will return.
`finish_sweep` then calls `saGetSweep_32f`, which TAKES the sweep and BLOCKS
until it is in (docs). Because of that the class sets `sweeps_in_finish`, and
the brain calls finish_sweep at once instead of idling for the estimated
sweep time first (which would double every sweep).

The thru reference for transmission is kept by the BRAIN (as the suite keeps
every reference), not with saStoreTgThru, so the GUI, the console and a scan
all divide by the same, inspectable trace, and a mismatch (other grid, other
TG level) is refused rather than silently applied. # VERIFY that a TG sweep
returns absolute dBm when no thru has been stored in the API; if it does not,
call saStoreTgThru(TG_THRU_0DB) once in `configure` on a thru and keep the
brain's reference anyway.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_float, c_int

import numpy as np

from .. import hwlock
from ..config import Config
from ..instruments import Grid, SweepSettings, estimate_sweep_time_s

# --- constants from sa_api.h ---------------------------------------------------
SA_IDLE = -1
SA_SWEEPING = 0x0
SA_TG_SWEEP = 0x4
SA_MIN_MAX = 0x0
SA_AVERAGE = 0x1
SA_LOG_SCALE = 0x0
TG_THRU_0DB = 0x1

SA_NO_ERROR = 0
SA_TG_NOT_FOUND = -10          # saTrackingGeneratorNotFound
SA_COMPRESSION_WARNING = 2     # saCompressionWarning: the input overloads the front end

DEVICE_TYPES = {0: "", 1: "SA44", 2: "SA44B", 3: "SA124A", 4: "SA124B"}

# name -> (restype, argtypes). Written from the header; ctypes checks the
# Python arguments against these before anything reaches the DLL.  # VERIFY
# on the installed DLL version (the API has kept these stable since 3.x).
_PROTOTYPES = {
    "saGetSerialNumberList": (c_int, [POINTER(c_int), POINTER(c_int)]),
    "saOpenDevice": (c_int, [POINTER(c_int)]),
    "saOpenDeviceBySerialNumber": (c_int, [POINTER(c_int), c_int]),
    "saCloseDevice": (c_int, [c_int]),
    "saGetSerialNumber": (c_int, [c_int, POINTER(c_int)]),
    "saGetDeviceType": (c_int, [c_int, POINTER(c_int)]),
    "saConfigAcquisition": (c_int, [c_int, c_int, c_int]),
    "saConfigCenterSpan": (c_int, [c_int, c_double, c_double]),
    "saConfigLevel": (c_int, [c_int, c_double]),
    "saConfigGainAtten": (c_int, [c_int, c_int, c_int, c_bool]),
    "saConfigSweepCoupling": (c_int, [c_int, c_double, c_double, c_bool]),
    "saInitiate": (c_int, [c_int, c_int, c_int]),
    "saAbort": (c_int, [c_int]),
    "saQuerySweepInfo": (c_int, [c_int, POINTER(c_int), POINTER(c_double), POINTER(c_double)]),
    "saGetSweep_32f": (c_int, [c_int, POINTER(c_float), POINTER(c_float)]),
    "saAttachTg": (c_int, [c_int]),
    "saIsTgAttached": (c_int, [c_int, POINTER(c_bool)]),
    "saConfigTgSweep": (c_int, [c_int, c_int, c_bool, c_bool]),
    "saStoreTgThru": (c_int, [c_int, c_int]),
    "saSetTg": (c_int, [c_int, c_double, c_double]),
    "saGetErrorString": (c_char_p, [c_int]),
    "saGetAPIVersion": (c_char_p, []),
}


#: The module name written into the hardware lock (the other service sees it).
LOCK_MODULE = "signalhound"


def lock_address(serial: int) -> str:
    """The PHYSICAL address of one analyser, for hwlock.

    A Signal Hound SA44B/SA124B has no VISA resource, COM port or IP: the API
    finds it on USB by its SERIAL NUMBER, which is therefore what names the
    box. Every service that could open this analyser must build the same
    string from the same serial, so it lives in one function."""
    return f"SIGNALHOUND::{int(serial)}"


class SaApiError(RuntimeError):
    """A negative saStatus from the DLL, with the API's own words for it."""


class SaApiAnalyzer:
    """A Signal Hound SA44B / SA124B, optionally with a USB-TG44A."""

    simulated = False
    #: The SA44B/SA124B take a sweep ON REQUEST, inside saGetSweep_32f (the
    #: "Modes of operation" page: "a sweep measured directly after the function
    #: is called"). So the brain must NOT idle for the sweep time before
    #: calling finish_sweep -- that would only double every sweep. The
    #: estimate is still used for the progress bar.
    sweeps_in_finish = True

    def __init__(self, cfg: Config, dll=None):
        """`dll` = an already loaded library (tests pass a fake); None = load
        sa_api.dll in open()."""
        self.cfg = cfg
        self._dll = dll
        self._h = None                       # the device handle (an int)
        self._idn = ""
        self._model = ""
        self._tg = False
        self._grid: Grid | None = None
        self._detector = "average"
        self._warnings: set[int] = set()
        self._pending = False
        self._lock: hwlock.HardwareLock | None = None   # our claim on the analyser

    # ---- plumbing ------------------------------------------------------------
    def _load(self):
        if self._dll is not None:
            return self._dll
        path = self.cfg.hardware.dll_path or "sa_api.dll"
        try:
            dll = ctypes.CDLL(path)          # VERIFY: cdecl (CDLL), not stdcall (WinDLL)
        except OSError as exc:
            raise SaApiError(
                f"cannot load {path!r} ({exc}). Install Signal Hound's Spike / SDK and "
                "put sa_api.dll on the PATH, or set hardware.dll_path") from None
        return dll

    def _bind(self, dll) -> None:
        for name, (restype, argtypes) in _PROTOTYPES.items():
            fn = getattr(dll, name, None)
            if fn is None:
                continue
            try:
                fn.restype, fn.argtypes = restype, argtypes
            except (AttributeError, TypeError):
                pass                          # a test fake that does not need them

    def _error_string(self, code: int) -> str:
        try:
            s = self._dll.saGetErrorString(code)
            return s.decode("ascii", "replace") if isinstance(s, bytes) else str(s)
        except Exception:
            return f"status {code}"

    def _call(self, name: str, *args) -> int:
        """Call one API function; a negative status raises, a positive one is a
        WARNING (compression, a clamped parameter) and is remembered."""
        st = int(getattr(self._dll, name)(*args))
        if st < 0:
            raise SaApiError(f"{name}: {self._error_string(st)} ({st})")
        if st > 0:
            self._warnings.add(st)
        return st

    # ---- lifecycle -------------------------------------------------------------
    def open(self) -> None:
        """Claim the analyser, open it, ask what it is.

        ONE INSTRUMENT, ONE SERVICE (Lukas's rule): the analyser is claimed in
        hwlock by its serial number before we talk to it, so a second service
        (another copy of this module on a second port pair, say) cannot drive
        the same box. With `hardware.serial` set the claim comes BEFORE the
        device is opened. With serial 0 ("the first analyser found") we only
        learn WHICH box it is after saOpenDevice, so we ask its serial -- a pure
        identity query -- and claim right then, before any other call; if that
        box is already claimed we close the handle again without sending
        saAbort (the box is not ours to stop).

        Every failure path releases the claim: a failed open must not leave the
        analyser marked busy."""
        hw = self.cfg.hardware
        try:
            if int(hw.serial):
                self._lock = hwlock.claim(lock_address(hw.serial), LOCK_MODULE)
            self._dll = self._load()
            self._bind(self._dll)
            h = c_int(-1)
            if int(hw.serial):
                self._call("saOpenDeviceBySerialNumber", byref(h), int(hw.serial))
            else:
                self._call("saOpenDevice", byref(h))    # the first unopened analyser
            self._h = int(h.value)
        except BaseException:
            self._release()
            raise
        try:
            sn = c_int(0)
            self._call("saGetSerialNumber", self._h, byref(sn))
            if self._lock is None:
                # Auto-discovered: claim the box we actually got. On HardwareBusy
                # close the handle WITHOUT saAbort (see the docstring).
                try:
                    self._lock = hwlock.claim(lock_address(sn.value), LOCK_MODULE)
                except hwlock.HardwareBusy:
                    self._close_handle(abort=False)
                    raise
            t = c_int(0)
            self._call("saGetDeviceType", self._h, byref(t))
            model = DEVICE_TYPES.get(int(t.value), "")
            self._model = {"SA44": "SA44B", "SA124A": "SA124B"}.get(model, model)
            if hw.model not in ("", "auto") and self._model != hw.model:
                raise SaApiError(f"connected analyser is a {model or 'unknown model'}, "
                                 f"but hardware.model asks for a {hw.model}")
            self._idn = f"Signal Hound {model} S/N {sn.value}"
            try:
                v = self._dll.saGetAPIVersion()
                self._idn += f", sa_api {v.decode('ascii', 'replace') if isinstance(v, bytes) else v}"
            except Exception:
                pass
            self._tg = False
            if hw.attach_tg:
                self._attach_tg()
        except BaseException:
            # saAbort only if the box is ours (claimed); then drop the claim.
            self._close_handle(abort=self._lock is not None)
            self._release()
            raise

    def _attach_tg(self) -> None:
        """Pair a TG44A if one is plugged in. None is not an error."""
        st = int(self._dll.saAttachTg(self._h))
        if st == SA_TG_NOT_FOUND:
            return
        if st < 0:
            raise SaApiError(f"saAttachTg: {self._error_string(st)} ({st})")
        ok = c_bool(False)
        self._call("saIsTgAttached", self._h, byref(ok))
        self._tg = bool(ok.value)
        # VERIFY: attaching must leave the TG output as it was (off, unless a
        # previous program left it emitting) until a TG sweep is initiated.
        # The API has no explicit "TG output off" call, and start-up does not
        # try to force one (start-up rule).

    def close(self) -> None:
        """Abort (which also stops a TG sweep, i.e. the TG output), close, and
        release our claim on the analyser. Errors are swallowed: this runs on
        crashes too."""
        try:
            self._close_handle(abort=True)
        finally:
            self._release()

    def _close_handle(self, abort: bool) -> None:
        """Close the device handle; `abort=False` sends ONLY saCloseDevice --
        used when the box turned out to belong to another service."""
        if self._h is None or self._dll is None:
            self._h = None
            return
        names = ("saAbort", "saCloseDevice") if abort else ("saCloseDevice",)
        for name in names:
            try:
                getattr(self._dll, name)(self._h)   # VERIFY: TG output stops on abort
            except Exception:
                pass
        self._h = None
        self._pending = False

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def idn(self) -> str:
        return self._idn

    def device_model(self) -> str:
        return self._model

    def tg_attached(self) -> bool:
        return self._tg

    # ---- sweeping ----------------------------------------------------------------
    def configure(self, s: SweepSettings) -> Grid:
        if self._h is None:
            raise SaApiError("analyser is not open")
        if s.tg_on and not self._tg:
            raise SaApiError("no tracking generator attached")
        h = self._h
        self._warnings.clear()
        self._pending = False
        self._call("saAbort", h)
        self._call("saConfigCenterSpan", h, s.center_Hz, s.span_Hz)
        detector = SA_MIN_MAX if s.detector == "peak" else SA_AVERAGE
        self._call("saConfigAcquisition", h, detector, SA_LOG_SCALE)     # dBm
        self._call("saConfigLevel", h, s.ref_level_dBm)
        self._call("saConfigGainAtten", h, int(s.atten), int(s.gain), bool(s.preamp))
        # VBW <= RBW is required by the API; the brain guarantees it.
        # VERIFY: which RBWs the API accepts at a large span (it may clamp:
        # saBandwidthClamped warning) -- the actual grid is reported below.
        self._call("saConfigSweepCoupling", h, s.rbw_Hz, s.vbw_Hz, bool(s.reject))
        if s.tg_on:
            # VERIFY: the API documents no TG output level for TG sweep mode.
            # saSetTg (frequency, amplitude in dBm) "can only be performed if a
            # tracking generator is paired ... and is currently NOT configured
            # and initiated for TG sweeps" (sa_api.h). So it goes HERE: after
            # saAbort (device idle) and BEFORE saConfigTgSweep, in the hope
            # that the TG sweep keeps that amplitude. Check with a power meter
            # on the TG output at two levels.
            self._call("saSetTg", h, s.center_Hz, s.tg_level_dBm)
            self._call("saConfigTgSweep", h, int(s.tg_points),
                       bool(s.tg_high_dynamic_range), bool(s.tg_passive_device))
            self._call("saInitiate", h, SA_TG_SWEEP, 0)
        else:
            # VERIFY: after a TG sweep (or the saSetTg above) the TG44A may
            # keep emitting a CW tone at its last frequency even in plain
            # spectrum mode -- the API has no "TG output off" call. Look for
            # a spur at the old centre with the TG on a spectrum sweep; if it
            # is there, the fix is to close and reopen the device here.
            self._call("saInitiate", h, SA_SWEEPING, 0)
        n, start, step = c_int(0), c_double(0.0), c_double(0.0)
        self._call("saQuerySweepInfo", h, byref(n), byref(start), byref(step))
        if n.value < 2:
            raise SaApiError(f"saQuerySweepInfo reported {n.value} bins")
        self._detector = s.detector
        self._grid = Grid(float(start.value), float(step.value), int(n.value))
        return self._grid

    def sweep_time_s(self, settings: SweepSettings, points: int) -> float:
        return estimate_sweep_time_s(settings, points)   # an estimate; no I/O

    def start_sweep(self) -> None:
        # VERIFY: the SA44B/SA124B sweep ON REQUEST (the sweep is taken inside
        # saGetSweep), so a sweep "starts" when finish_sweep asks for it -- which
        # is after the brain's trigger, so an acquisition is fresh. If the API
        # turns out to buffer a sweep taken earlier, discard the first
        # saGetSweep after each configure/trigger here.
        if self._grid is None:
            raise SaApiError("start_sweep before configure")
        self._pending = True

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise SaApiError("finish_sweep without start_sweep")
        self._pending = False
        n = self._grid.points
        mn = np.zeros(n, dtype=np.float32)
        mx = np.zeros(n, dtype=np.float32)
        before = set(self._warnings)
        self._warnings.discard(SA_COMPRESSION_WARNING)
        self._call("saGetSweep_32f", self._h,
                   mn.ctypes.data_as(POINTER(c_float)), mx.ctypes.data_as(POINTER(c_float)))
        overload = SA_COMPRESSION_WARNING in self._warnings
        self._warnings |= before - {SA_COMPRESSION_WARNING}
        # AVERAGE detector: min and max are the same array (docs). Peak: the max.
        trace = (mx if self._detector == "peak" else mn).astype(float)
        return trace, {"overload": overload}

    def abort_sweep(self) -> None:
        # saGetSweep has not been called yet, so there is nothing to throw away
        # on the instrument; the next configure aborts anyway.
        self._pending = False
