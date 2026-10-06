"""SpectrumAnalyzer: the brain between the wire and the backend (simulated or real).

Its SETTINGS (centre, span, reference level, RBW, VBW, detector, averages)
are set-and-forget: clamp, store, report. Its MEASUREMENT is a trace, and a
scan must never record a trace that was swept before the scan step it is filed
under -- the fire-and-forget contract makes that easy to get wrong, and
nothing raises when it happens.

Two ways to get a trace (the suite's rules for slow detectors):

  continuous  the analyser sweeps on its own, like a front panel; the latest
              trace is `get_trace("last")`. Right for looking at it.

  acquire     the scan-safe read. `acquire()` returns an id at once. The sweep
              thread then averages the next `sweep.averages` sweeps that
              STARTED after the trigger and latches the mean as the sample:
              `get_trace("sample")` + `status().sample`. Callers wait until
              status shows `acq_id` == their id AND `acquiring` False.

Averaging is done in linear POWER (mW), not in dB: averaging dB values of
noise reads ~2.5 dB low, and a spectrum analyser's "power average" is what
people expect.

A settings change in the middle of an acquisition RESTARTS it: averaging a
sweep at 100 kHz RBW with one at 1 kHz is not a measurement of anything.

THE GRID. The analyser -- not we -- decides the frequency bins (from span and
RBW). The brain re-`configure`s the backend when a setting changed and keeps
the Grid it returns; `frequencies()` gives it to a scan, and every trace
carries start/bin/points so it can be rebuilt exactly.

THE TRACKING GENERATOR -- THIS MODULE OWNS IT, OTHERS USE IT (Lukas's
decision 2026-09-28). The USB-TG44A can only be driven through the analyser's
API handle, and one physical instrument has one service. So the Signal Hound
kit is three modules: this one (the spectrum analyser and the ONLY owner of
the USB devices), `shsg` (the TG as a CW signal generator) and `shsna` (a
scalar network analyser: TG sweeps, thru, transmission). The two clients ask
this brain, over the wire, with four verbs:
  tg_cw             CW on/off, frequency, level. Refused while a TG sweep holds
                    the TG. A CW stays on while spectra are swept (measured
                    2026-09-28; `hardware.tg_cw_during_sweep` -- if False, a
                    CW PAUSES spectrum sweeping). "Off" is a PARK: the TG44A
                    has no off (below).
  tg_sweep_acquire  ONE TG-sweep acquisition, run in the sweep thread. It is
                    EXCLUSIVE: spectrum sweeping and the CW pause, and both are
                    restored afterwards -- the spectrum configuration first,
                    then the CW (or the park). The trace is dB relative to the
                    TG's calibrated output; the level is ignored (measured).
  get_tg_trace      the last finished TG acquisition.
  tg_abort          stop a queued or running one (latched as aborted).
The thru reference and transmission are shsna's business, not this brain's.

THERE IS NO "TG OFF" (measured on the TG44A, 2026-09-28): saAbort, closing the
device, even exiting the program leave the TG emitting its last frequency and
level, and after a TG sweep it sits at the last swept frequency. Lukas's
decision: "off" = PARK -- set the TG to hardware.tg_park_hz / tg_park_dbm (the
lowest level, far below anything measured). tg_cw(on=False), shutdown, and
the restore after a TG sweep without a CW all park it; status says "parked".

THE MODEL. What is connected (SA44B: 1 Hz - 4.4 GHz, RBW up to 250 kHz;
SA124B: 100 kHz - 12.4 GHz, up to 6 MHz) is read when the analyser opens, and
the frequency envelope follows it. describe's limits (and so its revision) follow.

Threads and locks (the pm16/hf2/vna rules):
  * ONE sweep thread talks to the backend for sweeps (spectrum and TG).
    `status()` only copies what it stored and never touches the hardware
    (gotcha #1). The CW verb calls the backend from the caller's thread,
    under the same hardware lock.
  * Every backend call runs under `_hw`, but the WAIT during a sweep does not.
  * Safety: the TG is never switched ON at start (nothing is sent to it); on
    shutdown it is PARKED before the device is closed. A client that goes
    away does NOT park its CW -- the shsg module decides that.

START-UP (Lukas's rule, 2026-09-27: "read the instrument state on startup,
not to change anything"). An SA44B/SA124B keeps NO settings of its own: the
API holds centre, span, RBW ... in the host process and forgets them when the
device is closed, and there is no call to read them back (saQuerySweepInfo
answers only after WE have configured and initiated). So what can be read is
read and adopted -- model (and with it the frequency / RBW envelope), serial,
API version, whether a TG44A is paired -- and nothing is written: `start()` does not
configure, initiate or abort, the sweep thread stays idle, and `continuous` is
switched off unless `acquisition.sweep_on_start`. The first setter,
`set_continuous(True)`, an acquire or `frequencies()` (a scan asking for its
axis) configures the analyser; each of those is a deliberate request. Status
shows `configured` False and `points` 0 until then. What the TG is doing
cannot be read (saGetTgFreqAmpl only echoes what THIS handle set; the lab
found a TG44A emitting a CW another program had left on), so `tg_mode` starts
as "unknown" -- never a guessed "off" -- until a client commands it.
Because the analyser keeps nothing, the SERVICE remembers the sweep window
across a restart (remember.py, 2026-10-06): start() loads it into cfg.sweep --
still writing nothing to the analyser; it is what the first sweep will use.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, fields

import numpy as np

from . import physics
from .config import Config, Scene
from .instruments import (DETECTORS, Grid, SweepSettings, TG_LEVEL_DBM, TG_RANGE_HZ,
                          model_range, sim_grid, snap_rbw)

_NAN = float("nan")

#: Sanity envelope for the simulated scene, name -> (min, max). Loose on
#: purpose: it stops nonsense (a negative bandwidth), not experiments.
SCENE_LIMITS = {
    "tone_Hz": (1.0, 13e9),
    "tone_dBm": (-150.0, 20.0),
    "harmonic_dBc": (-120.0, 0.0),
    "danl_dBm_per_Hz": (-180.0, -100.0),
    "dut_center_Hz": (1e3, 13e9),
    "dut_bandwidth_Hz": (1e3, 5e9),
    "dut_order": (1, 8),
    "dut_loss_dB": (0.0, 40.0),
    "cable_loss_dB_at_1GHz": (0.0, 20.0),
    "tg_ripple_dB": (0.0, 5.0),
}
SCENE_BOOLS = ("tone_on", "dut_inserted", "tg_attached")

#: tg_mode values. "unknown": nothing has set the TG since start -- it may well
#: be emitting what another program left on. "parked": at hardware.tg_park_hz /
#: tg_park_dbm (the TG44A has no off). "cw": a CW for shsg. "sweep": a TG
#: sweep for shsna is queued or running.
TG_MODES = ("unknown", "parked", "cw", "sweep")


@dataclass
class Status:
    """One snapshot of the analyser, for status() and the wire. No arrays: the
    status goes out ten times a second; traces are fetched with get_trace /
    get_tg_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    device_model: str = ""
    tg_attached: bool = False
    # the envelope in force now (model, limits)
    freq_min_Hz: float = _NAN
    freq_max_Hz: float = _NAN
    # settings
    center_Hz: float = _NAN
    span_Hz: float = _NAN
    start_Hz: float = _NAN
    stop_Hz: float = _NAN
    ref_level_dBm: float = _NAN
    rbw_Hz: float = _NAN
    vbw_Hz: float = _NAN
    reject: bool = True
    detector: str = "average"
    averages: int = 1
    continuous: bool = True
    # False until something deliberately configured the analyser in this
    # session (start-up never does: see START-UP in the module docstring)
    configured: bool = False
    # the grid the analyser uses for the configured settings
    points: int = 0
    bin_Hz: float = _NAN
    grid_start_Hz: float = _NAN
    sweep_time_s: float = _NAN
    # what the sweep thread is doing
    sweeping: bool = False
    sweep_progress: float = 0.0
    # why spectrum sweeping is on hold ("" = it is not): a TG sweep for shsna,
    # or a CW on hardware that cannot sweep with it (tg_cw_during_sweep False)
    spectrum_paused: str = ""
    sweeps: int = 0
    trace_id: int = 0
    peak_Hz: float = _NAN              # of the latest sweep
    peak_dBm: float = _NAN
    floor_dBm: float = _NAN            # median of the latest sweep: the noise floor
    overload: bool = False
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)
    # ---- the tracking generator (the contract with shsg / shsna) ----------
    # Lower-case units on purpose: these names ARE the wire contract the two
    # client modules were built against; do not rename them.
    tg_mode: str = "unknown"           # one of TG_MODES
    tg_cw_on: bool = False             # the APPLIED CW (the echo a client settles on)
    tg_cw_freq_hz: float = _NAN        # the CW frequency / level: applied while
    tg_cw_level_dbm: float = _NAN      # tg_cw_on, KEPT for the next "on" while parked
    tg_park_hz: float = _NAN           # where "off" parks the TG
    tg_park_level_dbm: float = _NAN
    tg_acq_id: int = 0                 # last TG acquisition started (accepted)
    tg_acquiring: bool = False
    tg_progress: float = 0.0
    tg_sample_id: int = 0              # last TG acquisition that FINISHED with a trace
    tg_error: str = ""                 # why the last TG acquisition failed / was aborted
    # the simulated scene (empty on a real analyser)
    scene: dict = field(default_factory=dict)


def _clamp(value, lo, hi):
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf -- NaN passes every `<`/`>` clamp."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _hz(v: float) -> str:
    """A frequency the way a person says it (for messages)."""
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(v) >= scale:
            return f"{v / scale:.6g} {unit}"
    return f"{v:.6g} Hz"


def _peak_and_floor(freqs, db) -> tuple[float, float, float]:
    finite = np.isfinite(db)
    if not finite.any():
        return _NAN, _NAN, _NAN
    i = int(np.nanargmax(db))
    return float(freqs[i]), float(db[i]), float(np.nanmedian(db))


class SpectrumAnalyzer:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic,
                 memory=None):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        # remember.SweepMemory, or None (tests, the in-process GUI simulator):
        # the operator's sweep window, kept across a restart of the SERVICE
        # (the analyser itself keeps nothing -- see remember.py)
        self._memory = None
        if memory is not None:
            self.attach_memory(memory)
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, traces, acquisition

        self._connected = False
        self._idn = ""
        self._model = ""
        self._tg = False
        self._hw_error = ""
        self._last_err_emit = -1e9
        # ARMED = someone asked for something that needs the analyser
        # configured (a setter, continuous on, an acquire). Until then the
        # idle sweep thread must not configure it (start-up rule). A plain
        # bool, only ever set to True -- written by setters, read by the
        # thread; it is not part of a snapshot, so gotcha #1 does not apply.
        self._armed = False

        # everything below is written under _lock
        self._rev = 0                       # bumped by any change that alters a trace
        # bumped whenever a TG call UNDID the analyser's configuration from
        # another thread (idle = saAbort): a spectrum sweep started before it
        # is thrown away instead of read from an aborted device
        self._gen = 0
        self._configured: SweepSettings | None = None
        self._grid: Grid | None = None
        # the last spectrum configuration and its grid, kept through a TG
        # sweep so `frequencies()` can still answer without touching the TG
        self._spectrum_cache: tuple[SweepSettings, Grid] | None = None
        self._sweeping = False
        self._sweep_t0 = 0.0
        self._sweep_dt = 0.0
        self._sweeps = 0
        self._trace_id = 0
        self._last: dict | None = None      # latest trace
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}
        self._sample_trace: dict | None = None
        # the tracking generator (see the module docstring)
        self._tg_cw = {"on": False, "freq_hz": _NAN, "level_dbm": _NAN}
        self._tg_known = False              # False = nothing set it since start: "unknown"
        self._tg_req: dict | None = None    # the queued / running TG acquisition
        # A CW change that could not get the hardware at once (a long spectrum
        # sweep holds it inside saGetSweep): applied by the sweep thread's next
        # pass. Found on the lab PC 2026-09-28 -- waiting made the client time
        # out for a change that was then applied anyway.
        self._cw_pending: dict | None = None
        self._tg_acq_id = 0
        self._tg_sample_id = 0
        self._tg_error = ""
        self._tg_trace: dict | None = None  # the last finished TG acquisition
        self._tg_ended: dict | None = None  # the last one that ended WITHOUT a trace

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # set by a TG request, an acquisition or a queued CW change: the idle
        # sweep thread starts at once instead of after its 0.1 s wait
        self._wake = threading.Event()
        # monotonic time the last TG acquisition ended (see the idle pass)
        self._tg_last_end = float("-inf")
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    def attach_memory(self, memory) -> None:
        """Remember the sweep window in `memory` (a remember.SweepMemory).
        Before start(): start() loads it."""
        memory.on_error = lambda msg: self._emit("warn", msg)
        self._memory = memory

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", True))

    # ---- lifecycle -----------------------------------------------------------------

    def start(self, run: bool = True) -> None:
        """Open the analyser and start the sweep thread.

        `run=False` skips the thread, so a test can drive `step()` by hand."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._model = self.backend.device_model()
            self._tg = bool(self.backend.tg_attached())
        # The TG's state cannot be read (see START-UP): it is "unknown" until a
        # client sets it. SAFETY: nothing here sends it anything -- a CW is a
        # deliberate act of a client module (shsg).
        with self._lock:
            self._tg_known = False
            self._tg_cw = {"on": False, "freq_hz": _NAN, "level_dbm": _NAN}
        # START-UP RULE: do not configure the analyser with the saved
        # settings. Sweeping continuously would do exactly that, so it waits
        # for a deliberate "continuous on" unless sweep_on_start says otherwise.
        acq = self.cfg.acquisition
        if acq.continuous and not acq.sweep_on_start:
            acq.continuous = False
        # The REMEMBERED sweep window (remember.py) replaces the config's
        # [sweep] defaults. This only fills cfg -- nothing reaches the analyser
        # until the first deliberate sweep, so the start-up rule still holds.
        # Sanitised right below: the model just read decides what fits.
        remembered = self._memory.load_into(self.cfg.sweep) if self._memory else None
        self._sanitise_config()
        self._connected = True
        self._emit("info", f"connected: {self._idn}"
                   + ("  + tracking generator" if self._tg else "  (no tracking generator)"))
        if remembered is not None:
            self._emit(*remembered)
        if not acq.continuous:
            self._emit("info", "analyser left as opened (nothing configured); "
                               "Continuous or Acquire starts sweeping")
        if self._tg:
            self._emit("info", "tracking generator state unknown (it may be emitting what "
                               "another program left on); nothing sent to it")
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="signalhound-sweep",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop sweeping, park the tracking generator, disconnect. Safe to call twice."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        if self._memory is not None:
            self._memory.flush()            # the last change, if the throttle held it back
        try:
            with self._hw:
                if was and self._tg:
                    # SAFETY: PARK the TG before the device closes -- the TG44A
                    # has no off and keeps emitting after close (measured)
                    try:
                        self._park_hw()
                    except Exception:
                        pass
                self.backend.close()        # aborts the sweep, releases the device
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._sweeping = False
                self._configured = None
                self._tg_req = None
            if was:
                self._emit("info", "disconnected")

    # ---- the envelope ------------------------------------------------------------

    def freq_range(self) -> tuple[float, float]:
        """The frequencies allowed NOW: config limits and the model."""
        lim = self.cfg.limits
        fmin, fmax, _ = model_range(self._model or self.cfg.hardware.model)
        return max(lim.freq_min_Hz, fmin), min(lim.freq_max_Hz, fmax)

    def tg_sweep_range(self) -> tuple[float, float]:
        """Where a TG sweep can go: the TG44A's range AND the analyser's."""
        lo, hi = self.freq_range()
        return max(lo, TG_RANGE_HZ[0]), min(hi, TG_RANGE_HZ[1])

    def tg_level_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return (max(lim.tg_level_min_dBm, TG_LEVEL_DBM[0]),
                min(lim.tg_level_max_dBm, TG_LEVEL_DBM[1]))

    def rbw_max(self) -> float:
        _, _, rmax = model_range(self._model or self.cfg.hardware.model)
        return min(self.cfg.limits.rbw_max_Hz, rmax)

    def center_limits(self) -> tuple[float, float]:
        lo, hi = self.freq_range()
        ms = self.cfg.limits.min_span_Hz
        return lo + ms / 2, hi - ms / 2

    def span_limits(self) -> tuple[float, float]:
        """The widest span that still fits around the CURRENT centre."""
        lo, hi = self.freq_range()
        c = self.cfg.sweep.center_Hz
        ms = self.cfg.limits.min_span_Hz
        return ms, max(ms, 2 * min(c - lo, hi - c))

    # ---- sweep settings (each clamps, stores in cfg, reports) --------------------

    def set_center(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "centre"), *self.center_limits())
        self.cfg.sweep.center_Hz = v
        msg = f"centre {v / 1e9:.9g} GHz"
        sp, sc = _clamp(self.cfg.sweep.span_Hz, *self.span_limits())
        if sc:
            self.cfg.sweep.span_Hz = sp
            msg += f"; span narrowed to {sp / 1e6:.6g} MHz to stay in range"
        self._changed(msg, clamped or sc)

    def set_span(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "span"), *self.span_limits())
        self.cfg.sweep.span_Hz = v
        self._changed(f"span {v / 1e6:.6g} MHz", clamped)

    def set_start_stop(self, start_Hz: float, stop_Hz: float) -> None:
        """Start/stop, for those who think that way: stored as centre + span."""
        a, b = _finite(start_Hz, "start"), _finite(stop_Hz, "stop")
        if b <= a:
            raise ValueError(f"stop ({b:g} Hz) must be above start ({a:g} Hz)")
        lo, hi = self.freq_range()
        a2, b2 = max(a, lo), min(b, hi)
        clamped = (a2, b2) != (a, b)
        ms = self.cfg.limits.min_span_Hz
        if b2 - a2 < ms:
            a2, b2, clamped = (a2 + b2) / 2 - ms / 2, (a2 + b2) / 2 + ms / 2, True
        self.cfg.sweep.center_Hz = _clamp((a2 + b2) / 2, *self.center_limits())[0]
        self.cfg.sweep.span_Hz = _clamp(b2 - a2, *self.span_limits())[0]
        self._changed(f"{a2 / 1e9:.9g} - {b2 / 1e9:.9g} GHz", clamped)

    # START or STOP alone, the other end held: what a scan axis over the
    # start (or stop) frequency needs -- Lukas 2026-10-06 wanted them as scan
    # parameters next to centre and span. Stored as centre + span like the pair.
    def _ends(self) -> tuple[float, float]:
        sw = self.cfg.sweep
        return sw.center_Hz - sw.span_Hz / 2, sw.center_Hz + sw.span_Hz / 2

    def set_start(self, hz: float) -> None:
        self.set_start_stop(_finite(hz, "start"), self._ends()[1])

    def set_stop(self, hz: float) -> None:
        self.set_start_stop(self._ends()[0], _finite(hz, "stop"))

    def set_ref_level(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "reference level"), lim.ref_min_dBm, lim.ref_max_dBm)
        self.cfg.sweep.ref_level_dBm = v
        self._changed(f"reference level {v:g} dBm", clamped)

    def set_rbw(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "RBW"), self.cfg.limits.rbw_min_Hz, self.rbw_max())
        snapped = snap_rbw(v, self.rbw_max())
        clamped = clamped or not math.isclose(snapped, v, rel_tol=1e-9)
        self.cfg.sweep.rbw_Hz = snapped
        msg = f"RBW {snapped:g} Hz"
        if self.cfg.sweep.vbw_Hz > snapped:
            # VBW <= RBW is a rule of the API; a narrower RBW drags the VBW down
            self.cfg.sweep.vbw_Hz = snapped
            msg += f" (VBW follows to {snapped:g} Hz)"
        self._changed(msg, clamped)

    def set_vbw(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "VBW"), self.cfg.limits.rbw_min_Hz,
                            self.cfg.sweep.rbw_Hz)
        self.cfg.sweep.vbw_Hz = v
        self._changed(f"VBW {v:g} Hz" + (" (VBW cannot exceed RBW)" if v < hz else ""), clamped)

    def set_reject(self, on: bool) -> None:
        self.cfg.sweep.reject = bool(on)
        self._changed("image rejection " + ("on" if on else "off"), False)

    def set_detector(self, detector: str) -> None:
        detector = str(detector).lower()
        if detector not in DETECTORS:
            raise ValueError(f"detector must be one of {DETECTORS}, got {detector!r}")
        self.cfg.sweep.detector = detector
        self._changed(f"{detector} detector", False)

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.sweep.averages = int(v)
        self._changed(f"{int(v)} averages per acquisition", clamped)

    def set_continuous(self, on: bool) -> None:
        if on:
            self._armed = True
        self.cfg.acquisition.continuous = bool(on)
        self._emit("info", "continuous sweep on" if on else "sweep on trigger only")

    # ---- the simulated scene --------------------------------------------------------

    def set_scene(self, name: str, value) -> None:
        """Change the simulated world. Not a trace-altering SETTING -- like
        swapping a cable in the lab, it does not restart an acquisition."""
        if not self.simulated:
            raise ValueError("the scene exists only in the simulator")
        if name in SCENE_BOOLS:
            v = value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "1.0", "true", "yes", "on")
            setattr(self.cfg.scene, name, bool(v))
            if name == "tg_attached":
                self._tg = bool(v) and bool(self.backend.tg_attached())
                if not self._tg:
                    with self._lock:
                        self._tg_cw["on"] = False      # no TG, no CW
            self._emit("info", f"scene {name} = {bool(v)}")
            return
        if name not in SCENE_LIMITS:
            raise ValueError(f"unknown scene parameter {name!r}; one of "
                             f"{', '.join(list(SCENE_LIMITS) + list(SCENE_BOOLS))}")
        v, clamped = _clamp(_finite(value, name), *SCENE_LIMITS[name])
        if name == "dut_order":
            v = int(round(v))
        setattr(self.cfg.scene, name, v)
        self._emit("warn" if clamped else "info",
                   f"scene {name} = {v:g}" + (" (clamped)" if clamped else ""))

    # ---- the scan-safe read ------------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set.
        During a TG sweep (for shsna) it waits: spectrum sweeping is paused."""
        if not self._connected:
            raise ValueError("not connected")
        self._armed = True
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = self._new_acq(self._acq_id)
            self._wake.set()
            return self._acq_id

    def abort(self) -> None:
        """Cancel a running acquisition. It is latched as ABORTED rather than
        dropped: a caller waiting for "acq_id == n and not acquiring" would
        otherwise see exactly that and read the PREVIOUS trace."""
        with self._lock:
            a = self._acq
            if a is None:
                return
            self._acq = None
            self._sample = {"acq_id": a["id"], "aborted": True, "time": time.time()}
            self._sample_trace = None
        self._emit("warn", f"acquisition #{a['id']} aborted")

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    def get_trace(self, which: str = "sample", quantity: str = "trace") -> dict:
        """A trace and what it was measured under.

        which    "sample" (the latched acquisition, what a scan records) or
                 "last" (the newest sweep).
        quantity "trace" -- the power per bin in dBm. (Transmission against a
                 thru moved to the shsna module on 2026-09-28.)

        Raises ValueError when there is nothing honest to return."""
        if quantity != "trace":
            raise ValueError(f"quantity must be 'trace', got {quantity!r} (transmission "
                             "vs a thru is measured by the shsna module now)")
        with self._lock:
            if which == "last":
                t = self._last
                if t is None:
                    raise ValueError("no sweep finished yet")
            elif which == "sample":
                if self._sample.get("aborted"):
                    raise ValueError(f"acquisition #{self._sample['acq_id']} was aborted")
                t = self._sample_trace
                if t is None:
                    raise ValueError("no acquisition latched yet")
            else:
                raise ValueError(f"which must be 'sample' or 'last', got {which!r}")
            t = dict(t)
        t["freqs_Hz"] = Grid(t["start_Hz"], t["bin_Hz"], int(t["points"])).freqs()
        return t

    def frequencies(self) -> np.ndarray:
        """The bins the NEXT sweep will use (a scan reads it once, before
        starting). Configures the analyser first if a setting changed: the grid
        is the analyser's answer to the settings, not something we can compute.

        During a TG sweep the analyser belongs to that sweep: the grid is
        served from the last spectrum configuration if the settings are the
        same, and refused ("busy") if a new one would be needed."""
        self._armed = True
        want = SweepSettings.from_config(self.cfg)
        with self._lock:
            busy = self._tg_req is not None
            cache = self._spectrum_cache
        if busy:
            if cache is not None and cache[0] == want:
                return cache[1].freqs()
            raise ValueError("busy: TG sweep running (for shsna); the new frequency grid "
                             "can be read when it is done")
        with self._hw:
            self._ensure_configured()
        with self._lock:
            g = self._grid
        if g is None:
            raise ValueError("the analyser has not reported a frequency grid")
        return g.freqs()

    # ---- the tracking generator: the contract with shsg / shsna --------------------

    def tg_cw(self, on=None, freq_hz=None, level_dbm=None) -> dict:
        """The TG as a CW source (for shsg). Returns {on, freq_hz, level_dbm,
        deferred}: the state now APPLIED -- the same values status then shows
        (the echo to settle on) -- or, with deferred True, the state that will
        be applied when the running TG sweep ends.

        on=False PARKS the TG (it has no off); freq_hz / level_dbm are then
        the CW kept for the next "on", and tg_park_hz says where it sits.
        A missing frequency / level keeps the one in force; when there is none
        (the TG is still "unknown" after start) the PARK value is used, so any
        accepted call leaves the TG in a known state ("cw" or "parked").

        While a TG sweep holds the TG the call is ACCEPTED but DEFERRED (shsg
        asked for this: its clean shutdown must not be refused, and the owner
        must not restore a CW nobody owns any more): the sweep's restore
        applies it instead of the CW remembered at its start.

        Refused (ValueError) with no TG or out of range (the TG44A: 10 Hz -
        4.4 GHz, -30 ... -10 dBm)."""
        if not self._connected:
            raise ValueError("not connected")
        if not self._tg:
            raise ValueError("no tracking generator attached to this analyser")
        hw = self.cfg.hardware
        with self._lock:
            req = self._tg_req
            if self._cw_pending is not None:          # newest intent first
                cur = dict(self._cw_pending)
            elif req is not None and req.get("cw_after"):
                cur = dict(req["cw_after"])
            else:
                cur = dict(self._tg_cw)
        f = cur["freq_hz"] if freq_hz is None else _finite(freq_hz, "TG frequency")
        lvl = cur["level_dbm"] if level_dbm is None else _finite(level_dbm, "TG level")
        # on=None: keep on/off as it is (a retune while parked stays parked and
        # is remembered for the next "on"). After "unknown" that means parked.
        on = bool(cur.get("on", False)) if on is None else bool(on)
        if not math.isfinite(f):
            f = float(hw.tg_park_hz)            # nothing to keep yet: the park value
        if not math.isfinite(lvl):
            lvl = float(hw.tg_park_dbm)
        self._check_tg_freq(f)
        self._check_tg_level(lvl)
        target = {"on": on, "freq_hz": f, "level_dbm": lvl}
        # Never wait long for the hardware: the reply means ACCEPTED (suite
        # rule), the status echo says APPLIED. If a sweep holds the hardware,
        # queue the change for the sweep thread and answer now.
        if not self._hw.acquire(timeout=0.2):
            with self._lock:
                self._cw_pending = dict(target)
            self._wake.set()
            self._emit("info", "TG CW change queued until the running sweep ends")
            return {**target, "deferred": True}
        try:
            # _hw is held by the sweep thread through a TG sweep's restore AND
            # its latch, so a request is either still there (defer) or fully
            # gone (apply now) -- never "restored already but not latched".
            with self._lock:
                req = self._tg_req
                self._cw_pending = None               # this newer command wins
                if req is not None:
                    req["cw_after"] = dict(target)
            if req is not None:
                self._emit("info", f"TG CW request deferred until TG sweep #{req['id']} ends")
                return {**target, "deferred": True}
            out = self._apply_cw_hw(target)
        finally:
            self._hw.release()
        return {**out, "deferred": False}

    def _apply_pending_cw(self) -> None:
        """Sweep thread: apply a CW change queued while a sweep held the
        hardware. During a TG sweep it becomes that sweep's restore target."""
        with self._hw:
            with self._lock:
                target, self._cw_pending = self._cw_pending, None
                req = self._tg_req
                if target is not None and req is not None:
                    req["cw_after"] = dict(target)
                    return
            if target is not None:
                try:
                    self._apply_cw_hw(target)
                except ValueError:
                    pass                              # _apply_cw_hw reported it

    def _apply_cw_hw(self, target: dict) -> dict:
        """Set the TG to `target` (CW or park) and store the echo. Called with
        _hw held. Raises ValueError (and sets hw_error) if the hardware fails."""
        hw = self.cfg.hardware
        f, lvl, on = target["freq_hz"], target["level_dbm"], target["on"]
        try:
            if on:
                self._set_tg_hw(f, lvl)
            else:
                self._park_hw()
        except Exception as exc:
            # the old values stay in status (hw_error rule: keep the last good)
            self._report_hw_error(exc, "TG CW failed")
            raise ValueError(f"TG CW failed: {type(exc).__name__}: {exc}") from exc
        # gotcha #40: the echo is stored AFTER the hardware has it, so a
        # frame that shows the new CW is one from after the command
        with self._lock:
            self._tg_cw = {"on": bool(on), "freq_hz": f, "level_dbm": lvl}
            self._tg_known = True
            out = dict(self._tg_cw)
        self._emit("info", f"TG CW {_hz(f)} at {lvl:g} dBm ON" if on else
                   f"TG CW off: parked at {_hz(hw.tg_park_hz)}, {hw.tg_park_dbm:g} dBm")
        return out

    def tg_sweep_acquire(self, start_hz, stop_hz, level_dbm=None, rbw_hz=None, averages=None,
                         points=None) -> int:
        """Queue ONE TG-sweep acquisition (for shsna); returns its id at once.

        The sweep thread then: pauses spectrum sweeping and remembers the CW,
        configures the TG sweep, sweeps `averages` times (averaged in linear
        power), restores the spectrum configuration, then re-issues the CW (or
        parks the TG) -- and only THEN publishes the result and clears
        tg_acquiring, in one critical section (gotcha #28).

        `level_dbm` is optional and NOT APPLIED: the TG44A's sweep ignores it
        (measured); if given it must still be a level the TG can make.
        `points` (optional) = requested bins, default hardware.tg_sweep_points,
        CLAMPED to limits.tg_points_* (the API clamps to 1001 silently; here it
        is announced). `tg_request(n)` tells what was accepted."""
        lim = self.cfg.limits
        settings, want_pts = self._tg_sweep_settings(start_hz, stop_hz, level_dbm, rbw_hz,
                                                     points)
        n_pts, a, b = settings.tg_points, start_hz, stop_hz
        n_avg = 1 if averages is None else int(round(_finite(averages, "averages")))
        if not lim.averages_min <= n_avg <= lim.averages_max:
            raise ValueError(f"averages must be {lim.averages_min} ... {lim.averages_max}, "
                             f"got {n_avg}")
        with self._lock:
            if self._tg_req is not None:
                raise ValueError(f"busy: TG sweep #{self._tg_req['id']} is running")
            # id and "acquiring" change together (gotcha #17)
            self._tg_acq_id += 1
            n = self._tg_acq_id
            self._tg_req = {"id": n, "settings": settings, "averages": n_avg,
                            "points": int(n_pts), "running": False, "aborted": False,
                            "n": 0, "t0": 0.0, "dt": 0.0,
                            # wall-clock marks for the trace's timing breakdown
                            "t_queued": time.monotonic()}
            self._tg_error = ""
        self._wake.set()
        if n_pts != want_pts:
            self._emit("warn", f"TG sweep #{n}: {want_pts} points clamped to {n_pts} "
                               f"({lim.tg_points_min} ... {lim.tg_points_max})")
        self._emit("info", f"TG sweep #{n}: {_hz(a)} - {_hz(b)}, {n_pts} points, "
                           f"{n_avg} avg (spectrum sweeping paused)")
        return n

    def tg_grid(self, start_hz, stop_hz, points=None, rbw_hz=None) -> dict:
        """The grid a TG sweep with these settings WOULD use -- a query for
        shsna, so a scan can build its frequency axis before any sweep. Sweeps
        nothing and sends nothing to the analyser; the same validation and
        clamps as tg_sweep_acquire. {start_hz, bin_hz, points, predicted}.

        The simulator's grid is exact (predicted False). The real analyser
        tells its grid only after configuring (saQuerySweepInfo), which would
        disturb the spectrum, so its grid is PREDICTED from the documented
        rule: start + i * (stop - start) / (points - 1), predicted True.
        # VERIFY against the grid of a real TG sweep (get_tg_trace)."""
        settings, _ = self._tg_sweep_settings(start_hz, stop_hz, None, rbw_hz, points)
        g = sim_grid(settings, self.cfg.limits.max_bins)   # TG sweep: exactly the rule
        return {"start_hz": g.start_Hz, "bin_hz": g.bin_Hz, "points": int(g.points),
                "predicted": not self.simulated}

    def _tg_sweep_settings(self, start_hz, stop_hz, level_dbm, rbw_hz, points):
        """Validate a TG sweep request -> (SweepSettings, points asked for).
        Refuses what the TG / analyser cannot do; points are CLAMPED."""
        if not self._connected:
            raise ValueError("not connected")
        if not self._tg:
            raise ValueError("no tracking generator attached to this analyser")
        a, b = _finite(start_hz, "start"), _finite(stop_hz, "stop")
        if b <= a:
            raise ValueError(f"stop ({b:g} Hz) must be above start ({a:g} Hz)")
        lo, hi = self.tg_sweep_range()
        if a < lo or b > hi:
            raise ValueError(f"TG sweep {_hz(a)} - {_hz(b)} is outside {_hz(lo)} - {_hz(hi)} "
                             "(the TG44A and this analyser)")
        lim = self.cfg.limits
        if b - a < lim.min_span_Hz:
            raise ValueError(f"TG sweep span must be at least {lim.min_span_Hz:g} Hz")
        lvl = None
        if level_dbm is not None:
            lvl = _finite(level_dbm, "TG level")
            self._check_tg_level(lvl)
        rbw = float(self.cfg.sweep.rbw_Hz) if rbw_hz is None else _finite(rbw_hz, "RBW")
        if not lim.rbw_min_Hz <= rbw <= self.rbw_max():
            raise ValueError(f"RBW {rbw:g} Hz outside {lim.rbw_min_Hz:g} - {self.rbw_max():g} Hz")
        rbw = snap_rbw(rbw, self.rbw_max())
        want_pts = (int(self.cfg.hardware.tg_sweep_points) if points is None
                    else int(round(_finite(points, "points"))))
        n_pts = int(_clamp(want_pts, lim.tg_points_min, lim.tg_points_max)[0])
        return SweepSettings.for_tg_sweep(self.cfg, a, b, lvl, rbw, n_pts), want_pts

    def tg_request(self, n: int) -> dict:
        """What was accepted for TG acquisition #n while it is queued/running:
        {id, points, averages, level_applied}. (The service puts it in the
        tg_sweep_acquire reply.)"""
        with self._lock:
            req = self._tg_req
            if req is None or req["id"] != int(n):
                return {"id": int(n)}
            return {"id": req["id"], "points": req["points"], "averages": req["averages"],
                    "level_applied": False}

    def get_tg_trace(self, id=None) -> dict:
        """The last finished TG acquisition: {id, start_hz, bin_hz, points,
        stop_hz, unit "dB", db (numpy: transmission relative to the TG's
        calibrated output), level_dbm None + level_applied False (the TG sweep
        ignores the level), rbw_hz, averages, overload, time}.
        With `id`, it must be THAT one; refused if it has not finished, was
        aborted or failed (the message says which)."""
        with self._lock:
            t, req, ended, last = self._tg_trace, self._tg_req, self._tg_ended, self._tg_sample_id
            if id is None:
                if t is None:
                    raise ValueError("no TG sweep has finished yet")
                return dict(t)
            n = int(id)
            if t is not None and t["id"] == n:
                return dict(t)
            if req is not None and req["id"] == n:
                raise ValueError(f"TG sweep #{n} has not finished yet")
            if ended is not None and ended["id"] == n:
                raise ValueError(ended["why"])
            raise ValueError(f"TG sweep #{n} is not available (the last finished one is #{last})")

    def tg_abort(self, id=None) -> bool:
        """Abort the queued / running TG acquisition; returns whether one was
        aborted. With `id`, only if THAT one is queued / running (shsna aborts
        its own sweep, never another client's). LATCHED as aborted (tg_error
        says so; get_tg_trace of it is refused). A running one stops at its
        next chance -- a sweep already inside the analyser finishes first --
        and the SA is restored as after any TG sweep."""
        queued = False
        with self._lock:
            req = self._tg_req
            if req is None or (id is not None and req["id"] != int(id)):
                return False
            req["aborted"] = True
            if not req["running"]:
                # never started: nothing on the analyser to restore
                queued = True
                self._tg_req = None
                why = f"TG sweep #{req['id']} aborted"
                self._tg_ended, self._tg_error = {"id": req["id"], "why": why}, why
        self._emit("warn", f"TG sweep #{req['id']} aborted")
        if queued and req.get("cw_after"):
            # a CW request that waited for this sweep: apply it now
            with self._hw:
                try:
                    self._apply_cw_hw(req["cw_after"])
                except ValueError:
                    pass                          # hw_error already says why
        return True

    def _check_tg_freq(self, f: float) -> None:
        lo, hi = TG_RANGE_HZ
        if not lo <= f <= hi:
            raise ValueError(f"TG frequency {_hz(f)} is outside the TG44A's "
                             f"{_hz(lo)} - {_hz(hi)}")

    def _check_tg_level(self, lvl: float) -> None:
        lo, hi = self.tg_level_range()
        if not lo <= lvl <= hi:
            raise ValueError(f"TG level {lvl:g} dBm is outside {lo:g} ... {hi:g} dBm")

    def _idle_hw(self) -> None:
        """saAbort on the real analyser: stops whatever is initiated (a TG
        sweep must be stopped before saSetTg is allowed). Does NOT silence the
        TG. Drops the spectrum configuration: reconfigure next sweep, and
        throw away a spectrum sweep started before it (_gen). Called with _hw."""
        self.backend.idle()
        with self._lock:
            self._configured = None
            self._gen += 1

    def _set_tg_hw(self, f: float, lvl: float) -> None:
        """Put the TG at f / lvl. On hardware that cannot sweep with a CW on
        (hardware.tg_cw_during_sweep False) the analyser is put idle first --
        the state the API documents saSetTg for. Called with _hw held."""
        if not self.cfg.hardware.tg_cw_during_sweep:
            self._idle_hw()
        self.backend.set_tg_cw(f, lvl)

    def _park_hw(self) -> None:
        """The TG's "off": it has none, so it is PARKED (Lukas, 2026-09-28)."""
        hw = self.cfg.hardware
        self._set_tg_hw(float(hw.tg_park_hz), float(hw.tg_park_dbm))

    # ---- status ------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        c = self.cfg
        sw = c.sweep
        lo, hi = self.freq_range()
        settings = SweepSettings.from_config(c)
        with self._lock:
            a, g = self._acq, self._grid
            last = self._last or {}
            progress = 0.0
            if self._sweeping and self._sweep_dt > 0:
                progress = min(1.0, (self._clock() - self._sweep_t0) / self._sweep_dt)
            acq_progress = 0.0
            if a is not None:
                acq_progress = min(1.0, (a["n"] + progress * self._sweeping) / a["want"])
            points = g.points if g is not None else 0
            req = self._tg_req
            tg_progress = 0.0
            if req is not None and req["running"]:
                part = 0.0
                if req["dt"] > 0:
                    part = min(1.0, (self._clock() - req["t0"]) / req["dt"])
                tg_progress = min(1.0, (req["n"] + part) / req["averages"])
            cw = self._tg_cw
            if req is not None:
                mode = "sweep"
            elif cw["on"]:
                mode = "cw"
            elif not self._tg_known:
                mode = "unknown"
            else:
                mode = "parked"
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                simulated=self.simulated, device_model=self._model, tg_attached=self._tg,
                freq_min_Hz=lo, freq_max_Hz=hi,
                center_Hz=sw.center_Hz, span_Hz=sw.span_Hz,
                start_Hz=sw.center_Hz - sw.span_Hz / 2, stop_Hz=sw.center_Hz + sw.span_Hz / 2,
                ref_level_dBm=sw.ref_level_dBm, rbw_Hz=sw.rbw_Hz, vbw_Hz=sw.vbw_Hz,
                reject=bool(sw.reject), detector=sw.detector, averages=int(sw.averages),
                continuous=bool(c.acquisition.continuous),
                configured=self._configured is not None,
                points=int(points), bin_Hz=g.bin_Hz if g else _NAN,
                grid_start_Hz=g.start_Hz if g else _NAN,
                sweep_time_s=self.backend.sweep_time_s(settings, max(points, 2)),
                sweeping=self._sweeping, sweep_progress=progress,
                spectrum_paused=self._pause_reason_locked(),
                sweeps=self._sweeps, trace_id=self._trace_id,
                peak_Hz=last.get("peak_Hz", _NAN), peak_dBm=last.get("peak_dBm", _NAN),
                floor_dBm=last.get("floor_dBm", _NAN), overload=bool(last.get("overload", False)),
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=acq_progress,
                sample=dict(self._sample),
                tg_mode=mode, tg_cw_on=bool(cw["on"]), tg_cw_freq_hz=float(cw["freq_hz"]),
                tg_cw_level_dbm=float(cw["level_dbm"]),
                tg_park_hz=float(c.hardware.tg_park_hz),
                tg_park_level_dbm=float(c.hardware.tg_park_dbm),
                tg_acq_id=self._tg_acq_id, tg_acquiring=req is not None,
                tg_progress=tg_progress, tg_sample_id=self._tg_sample_id,
                tg_error=self._tg_error,
                scene=({f.name: getattr(c.scene, f.name) for f in fields(Scene)}
                       if self.simulated else {}),
            )

    def _pause_reason_locked(self) -> str:
        """Why spectrum sweeping is on hold ("" = it is not). Called with _lock held."""
        if self._tg_req is not None:
            return f"TG sweep #{self._tg_req['id']} for a client (shsna) running"
        if self._tg_cw["on"] and not self.cfg.hardware.tg_cw_during_sweep:
            return ("TG CW on and this analyser cannot sweep with it "
                    "(hardware.tg_cw_during_sweep)")
        return ""

    # ---- config (Settings dialog / wire) ------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire)."""
        self._sanitise_config()
        self._changed("settings applied", False)

    # ---- the sweep thread -------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()

    def step(self) -> bool:
        """One pass of the sweep thread: a queued TG acquisition first (it is
        exclusive), else one spectrum sweep if there is a reason to sweep.
        Returns True if a sweep (of either kind) finished. Public so tests can
        drive the analyser without the thread."""
        self._wake.clear()
        with self._lock:
            pending = self._cw_pending is not None
        if pending:
            self._apply_pending_cw()
        with self._lock:
            req = self._tg_req
            paused = self._pause_reason_locked()
            wanted = self._acq is not None or self.cfg.acquisition.continuous
        if req is not None:
            return self._tg_sweep(req)
        try:
            if paused or not wanted:
                # idle, but keep the reported grid honest after a setter --
                # only once something was actually set: an untouched analyser
                # is left as it was opened (start-up rule). Not while paused:
                # configuring would disturb what paused it.
                # ...and not within a short GRACE after a TG sweep: shsna's
                # next windowed request usually follows at once, and a spectrum
                # configure (0.15-0.45 s) started now would make it wait
                # (lab PC 2026-09-28: 'queued' 0.11-0.19 s).
                grace = time.monotonic() - self._tg_last_end < 0.5
                if self._armed and not paused and not grace:
                    with self._hw:
                        self._ensure_configured()
                self._wake.wait(0.1)
                return False
            return self._sweep_once()
        except Exception as exc:              # never let the sweep thread die
            with self._lock:
                self._sweeping = False
                self._configured = None       # reconfigure from scratch next time
            self._report_hw_error(exc)
            self._stop.wait(0.5)
            return False

    def _ensure_configured(self) -> None:
        """Configure the backend if the settings differ from what it runs.
        Called with _hw held."""
        s = SweepSettings.from_config(self.cfg)
        with self._lock:
            if s == self._configured:
                return
        grid = self.backend.configure(s)
        with self._lock:
            self._configured, self._grid = s, grid
            self._spectrum_cache = (s, grid)

    def _sweep_once(self) -> bool:
        with self._lock:
            rev0, acq0 = self._rev, self._acq_id
        t0 = self._clock()
        with self._hw:
            self._ensure_configured()
            with self._lock:
                settings, grid, gen0 = self._configured, self._grid, self._gen
            self.backend.start_sweep()
            dt = self.backend.sweep_time_s(settings, grid.points)
        with self._lock:
            self._sweeping, self._sweep_t0, self._sweep_dt = True, t0, dt
        # A backend that TAKES the sweep inside finish_sweep (the real Signal
        # Hound: saGetSweep sweeps on request) must not be kept waiting here:
        # idling for dt first would double every sweep. dt still drives the
        # progress bar, since `_sweeping` stays True through finish_sweep.
        wait_dt = 0.0 if getattr(self.backend, "sweeps_in_finish", False) else dt

        # Wait out the sweep WITHOUT the hardware lock. Abandon it the moment
        # it can no longer count: settings changed, a new trigger arrived, a
        # TG call undid the configuration, or a client wants the TG (a TG
        # sweep, or a CW this hardware cannot sweep with).
        while True:
            left = t0 + wait_dt - self._clock()
            with self._lock:
                abandoned = (self._rev != rev0 or self._acq_id != acq0 or self._gen != gen0
                             or bool(self._pause_reason_locked()))
            if self._stop.is_set() or abandoned:
                with self._hw:
                    self._discard_pending()
                with self._lock:
                    self._sweeping = False
                return False
            if left <= 0:
                break
            self._stop.wait(min(0.02, left))

        with self._hw:
            with self._lock:
                undone = self._gen != gen0
            if undone:                        # an idle (saAbort) undid the configuration meanwhile
                self._discard_pending()
                with self._lock:
                    self._sweeping = False
                return False
            db, meta = self.backend.finish_sweep()
        db = np.asarray(db, dtype=float)
        if db.size != grid.points:
            raise RuntimeError(f"sweep returned {db.size} bins, the grid has {grid.points}")
        pk_f, pk_db, floor = _peak_and_floor(grid.freqs(), db)
        trace = {"start_Hz": grid.start_Hz, "bin_Hz": grid.bin_Hz, "points": grid.points,
                 "stop_Hz": grid.stop_Hz, "center_Hz": settings.center_Hz,
                 "span_Hz": settings.span_Hz, "rbw_Hz": settings.rbw_Hz,
                 "vbw_Hz": settings.vbw_Hz, "ref_level_dBm": settings.ref_level_dBm,
                 "detector": settings.detector,
                 "trace": db, "peak_Hz": pk_f, "peak_dBm": pk_db, "floor_dBm": floor,
                 "overload": bool(meta.get("overload", False)), "time": time.time()}

        latched = None
        with self._lock:
            self._sweeping = False
            if self._rev != rev0:
                return False                  # changed during the final read-out
            self._hw_error = ""
            self._sweeps += 1
            self._trace_id += 1
            self._last = {**trace, "trace_id": self._trace_id}
            a = self._acq
            if a is not None and t0 >= a["t0"]:
                p = physics.dbm_to_mw(db)
                a["sum"] = p if a["sum"] is None else a["sum"] + p
                a["n"] += 1
                a["overload"] = a["overload"] or trace["overload"]
                if a["n"] >= a["want"]:
                    # Clear "acquiring" and publish the sample in the SAME
                    # critical section (gotcha #28): split, a status frame in
                    # between would say "#n finished" while `sample` is still
                    # the old one.
                    self._latch_locked(a, trace)
                    self._acq = None
                    latched = a
        if latched is not None and latched["overload"]:
            self._emit("warn", f"acquisition #{latched['id']}: the input OVERLOADED "
                               "(raise the reference level)")
        if trace["overload"] and not latched:
            self._report_overload()
        return True

    def _latch_locked(self, a: dict, last: dict) -> None:
        """Average an acquisition's sweeps (in power) and publish them as THE
        sample. Called with _lock held."""
        mean = physics.mw_to_dbm(a["sum"] / a["n"])
        freqs = Grid(last["start_Hz"], last["bin_Hz"], last["points"]).freqs()
        pk_f, pk_db, floor = _peak_and_floor(freqs, mean)
        meta = {k: last[k] for k in ("start_Hz", "bin_Hz", "points", "stop_Hz", "center_Hz",
                                     "span_Hz", "rbw_Hz", "vbw_Hz", "ref_level_dBm",
                                     "detector")}
        self._sample = {"acq_id": a["id"], "time": time.time(), "averages": a["n"], **meta,
                        "peak_Hz": pk_f, "peak_dBm": pk_db, "floor_dBm": floor,
                        "overload": bool(a["overload"])}
        self._sample_trace = {**self._sample, "trace": mean}

    # ---- the TG sweep (runs in the sweep thread) ------------------------------------------

    def _tg_sweep(self, req: dict) -> bool:
        """Run one queued TG acquisition from start to latch. See
        tg_sweep_acquire for the order of events. Returns True with a trace."""
        err = ""
        trace = None
        grid = None
        with self._hw:
            with self._lock:
                if self._tg_req is not req:   # aborted while it was still queued
                    return False
                req["running"] = True
                cw = dict(self._tg_cw)        # what to give back afterwards
                spectrum_was = self._configured is not None
                # from here on the analyser is not in spectrum mode any more
                self._configured = None
                self._gen += 1
            s = req["settings"]
            req["t_start"] = time.monotonic()
            try:
                grid = self.backend.configure(s)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
        req["t_configured"] = time.monotonic()
        if not err:
            try:
                trace = self._tg_collect(req, s, grid)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
        if err:
            self._report_hw_error(RuntimeError(err), "TG sweep failed")
        # The restore and the latch run under _hw, so a tg_cw that arrives in
        # between is not lost: it waits for _hw and then finds no request.
        with self._hw:
            if not self._stop.is_set():
                with self._lock:
                    # a CW request that came in DURING the sweep wins over the
                    # CW remembered at its start
                    after = dict(req["cw_after"]) if req.get("cw_after") else cw
                req["t_swept"] = time.monotonic()
                self._tg_restore(spectrum_was, after)
            if trace is not None:
                t_end = time.monotonic()
                # where the time of one TG acquisition goes (the lab PC measured
                # ~1.2 s of overhead around an 11-bin sweep: this says where)
                trace["timing_s"] = {
                    "queued": round(req["t_start"] - req["t_queued"], 4),
                    "configure": round(req["t_configured"] - req["t_start"], 4),
                    "sweep": round(req.get("t_swept", t_end) - req["t_configured"], 4),
                    "restore": round(t_end - req.get("t_swept", t_end), 4),
                    "total": round(t_end - req["t_queued"], 4)}
            # ONE critical section: the result appears and tg_acquiring clears
            # together (gotcha #28) -- and only after the restore, so a client
            # that sees "done" finds the TG and the SA back as they were.
            self._tg_latch(req, trace, err)
        return trace is not None and not req["aborted"]

    def _tg_latch(self, req: dict, trace, err: str) -> None:
        n = req["id"]
        self._tg_last_end = time.monotonic()
        with self._lock:
            if self._tg_req is req:
                self._tg_req = None
            if trace is not None and not req["aborted"]:
                self._tg_trace = trace
                self._tg_sample_id = n
                self._tg_error = ""
                why = ""
            else:
                why = f"TG sweep #{n} failed: {err}" if err else f"TG sweep #{n} aborted"
                self._tg_ended = {"id": n, "why": why}
                self._tg_error = why
        if why:
            self._emit("warn", why)
        else:
            self._emit("info", f"TG sweep #{n} done"
                               + ("  (OVERLOAD)" if trace["overload"] else ""))

    def _tg_abandoned(self, req: dict) -> bool:
        return bool(req["aborted"]) or self._stop.is_set()

    def _tg_collect(self, req: dict, s: SweepSettings, grid: Grid) -> dict | None:
        """Sweep `averages` times and average in linear power. None if aborted."""
        total, n, over = None, 0, False
        for _ in range(req["averages"]):
            if self._tg_abandoned(req):
                return None
            t0 = self._clock()
            with self._hw:
                self.backend.start_sweep()
                dt = self.backend.sweep_time_s(s, grid.points)
            with self._lock:
                req["t0"], req["dt"] = t0, dt
            wait_dt = 0.0 if getattr(self.backend, "sweeps_in_finish", False) else dt
            while True:                       # the wait, WITHOUT the hardware lock
                if self._tg_abandoned(req):
                    with self._hw:
                        self._discard_pending()
                    return None
                left = t0 + wait_dt - self._clock()
                if left <= 0:
                    break
                self._stop.wait(min(0.02, left))
            with self._hw:
                db, meta = self.backend.finish_sweep()
            db = np.asarray(db, dtype=float)
            if db.size != grid.points:
                raise RuntimeError(f"TG sweep returned {db.size} bins, the grid has "
                                   f"{grid.points}")
            p = physics.dbm_to_mw(db)
            total = p if total is None else total + p
            n += 1
            over = over or bool(meta.get("overload", False))
            with self._lock:
                req["n"] = n
        if self._tg_abandoned(req):
            return None
        # dB relative to the TG's calibrated output (measured: not dBm); the
        # level was not applied, so none is reported
        return {"id": req["id"], "start_hz": grid.start_Hz, "bin_hz": grid.bin_Hz,
                "points": int(grid.points), "stop_hz": grid.stop_Hz,
                "unit": "dB", "level_dbm": None, "level_applied": False,
                "rbw_hz": s.rbw_Hz, "averages": n, "overload": over,
                "db": physics.mw_to_dbm(total / n), "time": time.time()}

    def _tg_restore(self, spectrum_was: bool, cw: dict) -> None:
        """Give the TG back as it was before the TG sweep: stop the TG sweep
        (idle), then the CW -- or, with no CW, the PARK: after a TG sweep the
        TG sits at the last swept frequency (measured), so it is always set to
        something.

        The SPECTRUM configuration is NOT restored here any more (2026-09-28,
        lab PC: ~1.2 s overhead per windowed acquisition, a spectrum configure
        is 0.15-0.45 s of it). It stays None; the sweep thread reconfigures it
        on its next idle pass or when a spectrum sweep is wanted -- AFTER the
        TG result is out, and not at all between back-to-back TG sweeps (a
        queued TG request is served first). A spectrum that was never
        configured stays unconfigured (start-up rule), as before.
        # VERIFY on the SA44B: a CW set right after saAbort (before the
        # spectrum's configure + initiate) survives that re-initiate -- the
        # lab measured that a CW survives aborts and spectrum sweeps."""
        try:
            with self._hw:
                self._idle_hw()
                if cw["on"]:
                    self.backend.set_tg_cw(cw["freq_hz"], cw["level_dbm"])
                else:
                    hw = self.cfg.hardware
                    self.backend.set_tg_cw(float(hw.tg_park_hz), float(hw.tg_park_dbm))
                with self._lock:
                    # the echo, AFTER the hardware has it (gotcha #40)
                    self._tg_cw = {"on": bool(cw["on"]), "freq_hz": cw["freq_hz"],
                                   "level_dbm": cw["level_dbm"]}
                    self._tg_known = True
        except Exception as exc:
            with self._lock:
                # say what IS true: the CW is not back
                self._tg_cw["on"] = False
                self._configured = None
            self._report_hw_error(exc, "restoring the analyser after a TG sweep failed")

    # ---- internals ----------------------------------------------------------------------------

    def _new_acq(self, acq_id: int) -> dict:
        return {"id": acq_id, "t0": self._clock(), "want": max(1, int(self.cfg.sweep.averages)),
                "n": 0, "sum": None, "overload": False}

    def _changed(self, msg: str, clamped: bool) -> None:
        """A trace-altering change: bump the revision and restart a running
        acquisition from scratch under the new settings."""
        restarted = None
        self._armed = True
        with self._lock:
            self._rev += 1
            if self._acq is not None:
                restarted = self._acq["id"]
                self._acq = self._new_acq(restarted)
        if self._memory is not None:
            self._memory.note(self.cfg.sweep)   # throttled; only what changed is written
        self._emit("warn" if clamped else "info", msg + (" (clamped)" if clamped else ""))
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _fit_window(self) -> None:
        """Pull centre and span inside the envelope in force now."""
        sw = self.cfg.sweep
        sw.center_Hz = _clamp(float(sw.center_Hz), *self.center_limits())[0]
        sw.span_Hz = _clamp(float(sw.span_Hz), *self.span_limits())[0]

    def _discard_pending(self) -> None:
        try:
            self.backend.abort_sweep()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        sw, lim, hw = self.cfg.sweep, self.cfg.limits, self.cfg.hardware
        self._fit_window()
        sw.ref_level_dBm = _clamp(float(sw.ref_level_dBm), lim.ref_min_dBm, lim.ref_max_dBm)[0]
        sw.rbw_Hz = snap_rbw(_clamp(float(sw.rbw_Hz), lim.rbw_min_Hz, self.rbw_max())[0],
                             self.rbw_max())
        sw.vbw_Hz = _clamp(float(sw.vbw_Hz), lim.rbw_min_Hz, sw.rbw_Hz)[0]
        sw.averages = int(_clamp(int(sw.averages), lim.averages_min, lim.averages_max)[0])
        sw.detector = str(sw.detector).lower()
        if sw.detector not in DETECTORS:
            sw.detector = "average"
        hw.tg_sweep_points = int(_clamp(int(hw.tg_sweep_points), lim.tg_points_min,
                                        lim.tg_points_max)[0])
        # the park must be a setting the TG can make
        hw.tg_park_hz = _clamp(float(hw.tg_park_hz), *TG_RANGE_HZ)[0]
        hw.tg_park_dbm = _clamp(float(hw.tg_park_dbm), *self.tg_level_range())[0]
        for name, (lo, hi) in SCENE_LIMITS.items():
            v = _clamp(float(getattr(self.cfg.scene, name)), lo, hi)[0]
            setattr(self.cfg.scene, name, int(round(v)) if name == "dut_order" else v)

    def _report_overload(self) -> None:
        now = self._clock()
        if now - getattr(self, "_last_over_emit", -1e9) >= 5.0:   # one per 5 s
            self._last_over_emit = now
            self._emit("warn", "input OVERLOAD: signal above the reference level "
                               "(raise the reference level)")

    def _report_hw_error(self, exc: Exception, what: str = "sweep failed") -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"{what}: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
