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
First run on a real SA44B + USB-TG44A on 2026-09-28 (sa_api 3.2.4); what
was measured is written next to each call below. Still unconfirmed: saStoreTgThru
(# VERIFY 6).

START-UP (Lukas's rule, 2026-09-27: read, do not change). `open()` only
opens the device and asks what it is: saGetDeviceType, saGetSerialNumber,
saGetAPIVersion, and -- the one exception -- saAttachTg + saIsTgAttached.
The analyser keeps no settings of its own (the API holds them in the host
process and has no getter for them), so there is nothing else to adopt, and
nothing is configured, initiated or aborted until the brain is asked to sweep.
saAttachTg PAIRS the TG44A with this handle; it is the only way to learn
whether a TG is there, and it does NOT change the TG output (measured
2026-09-28: a CW tone left on by another program was unchanged after
saAttachTg, 0.14 s). `hardware.attach_tg = False` skips it.

THE TG44A HAS NO "OFF" IN THIS API (measured 2026-09-28): once it has been set
(saSetTg, or a TG sweep, which leaves it at the LAST swept frequency) it keeps
emitting CW -- through saAbort, saCloseDevice and after the program has exited.
Only another setting, or unplugging it, changes that.

How a sweep goes: `configure` aborts whatever runs, sends every setting,
`saInitiate`s the mode and asks `saQuerySweepInfo` which bins it will return.
`finish_sweep` then calls `saGetSweep_32f`, which TAKES the sweep and BLOCKS
until it is in (docs). Because of that the class sets `sweeps_in_finish`, and
the brain calls finish_sweep at once instead of idling for the estimated
sweep time first (which would double every sweep).

The thru reference for transmission is kept by the BRAIN (as the suite keeps
every reference), not with saStoreTgThru, so the GUI, the console and a scan
all divide by the same, inspectable trace, and a mismatch (other grid, other
TG level) is refused rather than silently applied. MEASURED 2026-09-28: a TG
sweep does NOT return absolute dBm but TRANSMISSION in dB relative to the TG's
factory-calibrated output: through a 20 dB pad it read -19.4 dB flat over
900-1100 MHz, the same at TG level -30 and -20 dBm. # VERIFY 6: whether
saStoreTgThru(TG_THRU_0DB) then moves it to ~0 dB (inconclusive: the test
reconfigured between storing and sweeping).
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
SA_DEVICE_NOT_FOUND = -8       # saDeviceNotFoundErr -- also "already open elsewhere"
SA_COMPRESSION_WARNING = 2     # saCompressionWarning: the input overloads the front end

DEVICE_TYPES = {0: "", 1: "SA44", 2: "SA44B", 3: "SA124A", 4: "SA124B"}

# name -> (restype, argtypes). Written from the header; ctypes checks the
# Python arguments against these before anything reaches the DLL. Checked
# 2026-09-28 against the installed sa_api.dll 3.2.4 (Spike): every name is
# exported, and open / query / configure / sweep work with these types on an
# SA44B. The header itself is not installed with Spike, so the TG prototypes
# and the TG calls (saAttachTg, saIsTgAttached, saSetTg, saConfigTgSweep,
# saStoreTgThru) work with these types on a USB-TG44A.
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

#: Where Signal Hound's installers put sa_api.dll. Spike (the usual install on
#: a lab PC) keeps it in its own folder, which is NOT on the PATH -- found on
#: the lab PC 2026-09-28, where the service could not start without this.
DLL_SEARCH = [r"C:\Program Files\Signal Hound\Spike\sa_api.dll"]

# Sweep time of the SA44B in spectrum mode, fitted to sweeps measured on the
# lab's analyser (2026-09-28, sa_api 3.2.4): ~135 MHz of span per second,
# almost independent of the RBW down to 10 Hz, plus a cost per output bin (the
# FFT work of a narrow RBW shows up as MORE BINS, not as a slower span rate)
# and a fixed overhead. Within ~2x of every measurement (e.g. 4.3 GHz: 32 s;
# RBW 10 Hz over 100 kHz: 0.68 s); the rule it replaces was off by up to 1000x.
_SPAN_RATE_HZ_PER_S = 135e6
_TIME_PER_BIN_S = 1.0e-5
_SWEEP_OVERHEAD_S = 0.05


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
        self._ref_level = float("inf")       # set by configure; inf = never "above"
        self._warnings: set[int] = set()
        self._pending = False
        self._lock: hwlock.HardwareLock | None = None   # our claim on the analyser

    # ---- plumbing ------------------------------------------------------------
    def _load(self):
        if self._dll is not None:
            return self._dll
        # CDLL: the DLL is 64-bit, where cdecl and stdcall are the same calling
        # convention (checked 2026-09-28: x86-64 PE, all 22 functions exported
        # undecorated).
        # An explicit hardware.dll_path is used as given; with none, the PATH
        # first, then Spike's install folder.
        explicit = self.cfg.hardware.dll_path
        paths = [explicit] if explicit else ["sa_api.dll", *DLL_SEARCH]
        errors = []
        for path in paths:
            try:
                return ctypes.CDLL(path)
            except OSError as exc:
                errors.append(f"{path!r} ({exc})")
        raise SaApiError(
            f"cannot load sa_api.dll: {'; '.join(errors)}. Install Signal Hound's Spike / SDK "
            "and put sa_api.dll on the PATH, or set hardware.dll_path") from None

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
            try:
                if int(hw.serial):
                    self._call("saOpenDeviceBySerialNumber", byref(h), int(hw.serial))
                else:
                    self._call("saOpenDevice", byref(h))    # the first unopened analyser
            except SaApiError as exc:
                # "Device not found" also means "found, but already open": the
                # API opens a box only once per PC, so a second service (or
                # Spike) holding it looks exactly like no box at all. Seen on
                # the lab PC 2026-09-28 -- a second service with serial 0 never
                # got as far as the hardware lock, whose message says "busy".
                if f"({SA_DEVICE_NOT_FOUND})" in str(exc):
                    raise SaApiError(
                        f"{exc} -- if the analyser is plugged in, another AaltoFlow service "
                        "or Spike may hold it (one program per analyser)") from None
                raise
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
        # Measured 2026-09-28: attaching leaves the TG output as it was. The
        # API has no "TG output off" call, and start-up does not try to force
        # one (start-up rule). NOTE: saGetTgFreqAmpl reports only what THIS
        # handle has set (0 Hz / 0 dBm right after attaching, although the TG
        # was emitting -30 dBm at 1 GHz) -- it cannot read the TG's real state.

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
                # saAbort stops a sweep but NOT the TG output (measured
                # 2026-09-28): the TG keeps emitting its last CW tone after
                # close. There is no "off" to send here.
                getattr(self._dll, name)(self._h)
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
        # Measured 2026-09-28 (SA44B): 100 kHz and 250 kHz are accepted even
        # over a 4.3 GHz span with no saBandwidthClamped warning, and 10 Hz
        # over 100 kHz works. An unsnapped RBW (150 kHz) is accepted SILENTLY
        # and gives the same grid as 100 kHz -- which is why the brain snaps.
        self._call("saConfigSweepCoupling", h, s.rbw_Hz, s.vbw_Hz, bool(s.reject))
        if s.tg_on:
            # saSetTg is only allowed while the TG is NOT in TG sweep mode
            # (sa_api.h), hence here, after saAbort and before saConfigTgSweep.
            # MEASURED 2026-09-28: the TG sweep IGNORES this level (-30 and
            # -20 dBm gave identical traces) and returns transmission in dB, not
            # dBm. As a CW source (spectrum mode) the level IS honoured: -30 ->
            # -50.04, -20 -> -40.06 dBm through a 20 dB pad, 0.03 s per change.
            # tg_points: at most 1001 (5000 was cut to 1001 silently; the grid
            # is read back below). Sweep time ~0.2 s + 1.3 ms/point.
            self._call("saSetTg", h, s.center_Hz, s.tg_level_dBm)
            self._call("saConfigTgSweep", h, int(s.tg_points),
                       bool(s.tg_high_dynamic_range), bool(s.tg_passive_device))
            self._call("saInitiate", h, SA_TG_SWEEP, 0)
        else:
            # MEASURED 2026-09-28: after a TG sweep (or saSetTg) the TG44A
            # keeps emitting CW at its last frequency in plain spectrum mode --
            # it shows as a spur. Closing and reopening does NOT stop it (the
            # TG is not reset by the API), so there is no fix in this call;
            # it is the reason a CW source and spectrum sweeps CAN coexist.
            self._call("saInitiate", h, SA_SWEEPING, 0)
        n, start, step = c_int(0), c_double(0.0), c_double(0.0)
        self._call("saQuerySweepInfo", h, byref(n), byref(start), byref(step))
        if n.value < 2:
            raise SaApiError(f"saQuerySweepInfo reported {n.value} bins")
        self._detector = s.detector
        self._ref_level = float(s.ref_level_dBm)
        self._grid = Grid(float(start.value), float(step.value), int(n.value))
        return self._grid

    def sweep_time_s(self, settings: SweepSettings, points: int) -> float:
        """An estimate for the progress bar; never talks to the instrument
        (status() asks ten times a second). Spectrum mode uses the model
        measured on the SA44B (constants above); TG mode is not measured yet
        and keeps the generic estimate."""
        if settings.tg_on:
            return estimate_sweep_time_s(settings, points)
        t = (_SWEEP_OVERHEAD_S + settings.span_Hz / _SPAN_RATE_HZ_PER_S
             + _TIME_PER_BIN_S * max(int(points), 0))
        return float(min(t, 600.0))

    def start_sweep(self) -> None:
        # The SA44B sweeps ON REQUEST (the sweep is taken inside saGetSweep),
        # so a sweep "starts" when finish_sweep asks for it -- after the
        # brain's trigger, so an acquisition is fresh. Checked 2026-09-28:
        # saGetSweep's duration scales with the span (32 s for 4.3 GHz, i.e. it
        # sweeps then, it does not hand back a buffer), and consecutive sweeps
        # of the noise floor all differ (median |diff| ~3.9 dB).
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
        self._warnings |= before - {SA_COMPRESSION_WARNING}
        # AVERAGE detector: min and max are the same array (checked on the
        # SA44B 2026-09-28: identical to the last bit). Peak: the max array.
        trace = (mx if self._detector == "peak" else mn).astype(float)
        # Overload: the API's saCompressionWarning, OR any bin above the
        # reference level. On the SA44B the warning never came (2026-09-28): a
        # -50 dBm tone against a -60/-70/-80 dBm reference read 3 dB low --
        # compressed -- with status 0. The reference level is where the API
        # sets the front-end gain, so a signal above it is not to be trusted.
        overload = (SA_COMPRESSION_WARNING in self._warnings
                    or bool(np.nanmax(mx) > self._ref_level))
        return trace, {"overload": overload}

    def abort_sweep(self) -> None:
        # saGetSweep has not been called yet, so there is nothing to throw away
        # on the instrument; the next configure aborts anyway.
        self._pending = False

    # ---- the TG as a CW source (owner-side TG contract, added 2026-09-28) -----
    # This module is the only owner of the analyser AND its TG44A; the shsg
    # module (a CW source) and shsna (TG sweeps) ask for the TG over the wire.
    # A TG SWEEP needs nothing new here: it is `configure` with tg_on True.
    # MEASURED on the SA44B + TG44A (lab PC, 2026-09-28, sa_api 3.2.4):
    #   * saSetTg takes ~0.03 s, the level is honoured (-30 dBm read -50.04
    #     through a 20.0 dB pad) and a new frequency moves the tone.
    #   * a CW stays on while spectrum sweeps run (hardware.tg_cw_during_sweep).
    #   * there is NO "off": saAbort, saCloseDevice and even exiting the
    #     program leave the TG emitting its last frequency and level; the brain
    #     PARKS it (hardware.tg_park_hz / tg_park_dbm) instead.
    #   * saGetTgFreqAmpl only echoes what THIS handle set (0 Hz / 0 dBm after
    #     attach while a 1 GHz tone was on), so it cannot adopt a left-over
    #     state: start-up reports the TG as "unknown".
    #   * a TG sweep ignores the saSetTg level and returns dB relative to the
    #     TG's calibrated output; at most 1001 points (the API clamps silently).

    def set_tg_cw(self, freq_hz: float, level_dbm: float) -> None:
        """TG output: a CW tone at freq_hz, level_dbm.

        saSetTg is documented as allowed only while the TG is NOT configured
        and initiated for TG sweeps; the brain calls it only after a spectrum
        configure or an `idle` (saAbort), never in TG sweep mode."""
        if self._h is None:
            raise SaApiError("analyser is not open")
        if not self._tg:
            raise SaApiError("no tracking generator attached")
        # VERIFY: the level accuracy over the whole -30 ... -10 dBm and
        # 10 Hz ... 4.4 GHz (measured so far: -30 dBm, one frequency).
        self._call("saSetTg", self._h, float(freq_hz), float(level_dbm))

    def idle(self) -> None:
        """saAbort: stop whatever is initiated (a TG sweep must be stopped
        before saSetTg is allowed). Does NOT silence the TG (measured). Leaves
        the analyser unconfigured -- the brain reconfigures before its next
        sweep (spectrum mode re-initiates in 0.2 - 0.4 s, measured)."""
        if self._h is None or self._dll is None:
            return
        self._pending = False
        self._call("saAbort", self._h)
