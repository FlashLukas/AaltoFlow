"""The Chopper: the brain between the wire and the backend (simulated or real).

An optical chopper is a set-and-forget instrument with ONE thing that takes
time: after a new frequency (or after switching on) the wheel has to spin up
and the PLL has to lock, "within a few seconds" (manual 5.1). A scan stepping
the chopping frequency must wait for that, or it measures while the lock-in's
reference is still sliding. So besides holding and clamping the settings, the
brain's real job is to say honestly WHEN THE WHEEL IS LOCKED:

    locked = motor enabled
             AND |measured - target| <= max(tolerance_Hz, tolerance_rel * target)
             held continuously for settle.hold_s

`measured` is the wheel as seen by a slot sensor -- the REF OUT frequency
(refoutfreq?) when the reference output follows a sensor ("actual", "outer",
"inner"). With the output on "target" (the synthesiser) that number only
repeats the set value and the wheel is invisible; then the brain waits
`settle.blind_lock_s` after the last change instead and reports
`lock_source = "timer"`, so nobody mistakes a timer for a measurement.

`target` is the set frequency on internal reference, and EXT REF IN x N / D on
external reference (then the frequency is not ours to set; `describe` turns it
into an indicator).

A frequency SWEEP for fly scans (ramp_frequency, 2026-10-10): the SERVICE
walks the synthesiser frequency at a set pace (softramp.py) and the poll
thread -- faster while it runs -- records every REF OUT reading with its time,
so a fly scan bins by the MEASURED wheel frequency (by the commanded one,
honestly declared, while REF OUT sits on 'target' and the wheel is blind).
While the setpoint moves the wheel is never "locked"; at the end the lock is
judged afresh. A set, standby and a blade / reference change stop it.

What it deliberately does NOT do: stop or start the wheel at start-up. A
spinning chopper is harmless, and somebody's lock-in may be using it; the brain
ADOPTS blade, modes, frequency, phase and the run state. At shutdown it leaves
the wheel alone too, unless `hardware.stop_on_exit` is set.

Threads and locks (the rules the other modules learned the hard way):

  * ONE polling thread reads the controller. `status()` only copies what that
    thread stored and never touches the serial port, so a slow or dead link
    cannot stall the status publisher; a failed read shows as `hw_error`.
  * EVERY backend call runs under `_hw` (an RLock): the command thread and the
    polling thread must not talk over each other on one serial line.
  * A setter stores the new setpoint, clears the lock and bumps a command
    GENERATION in ONE critical section (gotcha #1 and #28). A poll that began
    before the command sees the generation move and does not judge the new
    setpoint with readings taken before it existed -- so no status frame can
    ever show the new frequency next to the old point's `locked = True`.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import ChopperBackend
from .blades import Blade, blade_by_index, blade_by_name, parse_owned
from .config import Config
from .softramp import SoftRamp
from .stream import StreamRecorder

_NAN = float("nan")

#: How `locked` is judged (status `lock_source`; describe's enum options):
#: from the slot sensor, or -- with REF OUT on 'target' -- by a timer.
LOCK_MEASURED = "measured"
LOCK_TIMER = "timer"
LOCK_SOURCES = (LOCK_MEASURED, LOCK_TIMER)


@dataclass
class Status:
    """One snapshot of the chopper, for status() and the wire."""

    connected: bool
    simulated: bool = True
    idn: str = ""
    hw_error: str = ""
    blade: str = ""
    ref_mode: str = ""
    output_mode: str = ""
    external: bool = False
    enabled: bool = False
    setpoint_frequency_Hz: float = _NAN     # what we asked for (internal reference)
    target_frequency_Hz: float = _NAN       # what the wheel should run at now
    frequency_Hz: float = _NAN              # MEASURED, on the referenced ring (NaN if blind)
    freq_error_Hz: float = _NAN
    refout_frequency_Hz: float = _NAN       # raw REF OUT reading
    input_frequency_Hz: float = _NAN        # EXT REF IN (external mode only)
    locked: bool = False
    lock_source: str = "measured"           # "measured" | "timer"
    lock_gen: int = 0                       # bumped by every change that needs a new lock
    phase_deg: float = 0.0
    nharmonic: int = 1
    dharmonic: int = 1
    freq_min_Hz: float = _NAN               # live range: blade ring AND the safety envelope
    freq_max_Hz: float = _NAN
    owned_blades: list = field(default_factory=list)
    readings: int = 0
    poll_ms: float = _NAN
    # the frequency SWEEP (ramp_frequency, fly scans): ramp_id = the newest
    # sweep started; it is over when ramp_id is yours and `ramping` is False
    ramping: bool = False
    ramp_id: int = 0
    ramp_target_Hz: float = _NAN
    ramp_rate_Hz_per_s: float = _NAN


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _equivalent_ref(old: str, blade: Blade) -> str:
    """The reference-in mode on `blade` that means the same as `old` on the
    previous blade: internal stays internal, external stays external. On a
    10/100 blade "internal" becomes "int-inner" (the ring a beam usually goes
    through); an unchanged mode name is kept as it is."""
    if old in blade.ref_modes:
        return old
    ext = old.startswith("ext")
    for cand in (("ext-inner", "external") if ext else ("int-inner", "internal")):
        if cand in blade.ref_modes:
            return cand
    return blade.ref_modes[0]


def _equivalent_output(old: str, ref: str, blade: Blade) -> str:
    """Keep REF OUT on a SENSOR if it was on one (so the wheel stays
    measurable); follow the ring the reference locks to on a 10/100 blade."""
    if old == "target":
        return "target"
    if "actual" in blade.output_modes:
        return "actual"
    ring = blade.ring_of(ref)
    return ring if ring in blade.output_modes else blade.output_modes[0]


class Chopper:
    #: every Nth poll also re-reads blade / modes / phase / harmonics, so a
    #: change made on the FRONT PANEL shows up within a couple of seconds
    FULL_READ_EVERY = 10

    def __init__(self, backend: ChopperBackend, cfg: Config | None = None,
                 clock=time.monotonic, simulated: bool = True):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._simulated = bool(simulated)
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards everything below
        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._blade: Blade = blade_by_name("MC1F10HP")
        self._ref = "internal"
        self._output = "target"
        self._nh = 1
        self._dh = 1
        self._enabled = False
        self._freq_sp = _NAN
        self._phase = 0.0
        self._refout = _NAN
        self._input = _NAN
        self._measured = _NAN
        self._gen = 0                       # bumped by every command that moves the wheel
        self._changed_at = clock()          # for the blind (timer) lock
        self._band_since = None             # when "inside the band" last became true
        self._locked = False
        self._readings = 0
        self._poll_ms = _NAN
        self._npoll = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

        # THE FREQUENCY SWEEP (suite_common/softramp.py, copied as softramp.py).
        # Live limits = the referenced ring AND the envelope, read every step.
        self._sent_hz = _NAN                # last value written by the sweep (grid)
        self._sweep = SoftRamp(self._sweep_step, lambda: self._freq_sp,
                               limits=self.freq_limits,
                               dt_s=float(getattr(self.cfg.hardware, "ramp_dt_s", 0.1)),
                               on_done=self._sweep_done, channel="commanded",
                               name="chopper-sweep")
        # THE STREAM (fly scans): every poll's REF OUT reading with its time --
        # the wheel frequency on the referenced ring (NaN while blind) and the
        # raw REF OUT value. The COMMANDED frequency comes from the sweep's own
        # record (softramp.py: every value sent, stamped when it was sent),
        # with its own time stamps (`t_ch`) -- sampling it at the poll would
        # lose the last step until the next poll.
        self.recorder = StreamRecorder(["frequency", "refout"])

    # ---- lifecycle ---------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Connect, ADOPT the controller's state (command nothing), start
        polling. `poll=False` is for tests that step `poll_once()` by hand."""
        with self._hw:
            # open() either succeeds (and, on real hardware, holds the COM
            # port's hwlock claim) or raises having released everything --
            # e.g. HardwareBusy when another service drives this chopper.
            self.backend.open()
            try:
                idn = self.backend.idn()
                state = self._read_config_locked()
            except BaseException:
                # Opened but could not read it: close, so the port and its
                # claim are not left held by a brain that never started. Only
                # close() -- no "safe state" command to a unit we never adopted.
                try:
                    self.backend.close()
                except Exception:
                    pass
                raise
        with self._lock:
            self._idn = idn
            self._apply_read(state)
            self._freq_sp = float(state["freq"])     # adopt the synthesiser setting
            self._connected = True
            self._hw_error = ""
            b, en, f = self._blade.name, self._enabled, self._freq_sp
        self._emit("info", f"connected: {idn or 'MC2000B'}; adopted blade {b}, "
                           f"{f:g} Hz, {'running' if en else 'standby'} (nothing commanded)")
        if b not in parse_owned(self.cfg.blades.owned):
            self._emit("warn", f"the controller is set for blade {b}, which is not in "
                               f"blades.owned ({self.cfg.blades.owned}) -- check the wheel")
        self.poll_once()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="chopper-poll", daemon=True)
            self._thread.start()

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop polling and disconnect. The wheel is LEFT RUNNING unless
        hardware.stop_on_exit is set. Safe to call more than once / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        the wheel is left as it is even with stop_on_exit set -- the next start
        adopts it."""
        self._sweep.stop()           # no sweep step may follow (before any lock)
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            if was and bool(self.cfg.hardware.stop_on_exit) and not keep_outputs:
                with self._hw:
                    self.backend.set_enable(False)
                self._emit("info", "chopper disabled on exit (hardware.stop_on_exit)")
        except Exception as exc:
            self._emit("error", f"could not disable on exit: {exc}")
        finally:
            try:
                with self._hw:
                    self.backend.close()
            finally:
                with self._lock:
                    self._connected = False
                    self._locked = False
                if was:
                    self._emit("info", "disconnected")

    # ---- reading the controller ---------------------------------------------

    def _read_config_locked(self) -> dict:
        """Everything that is not a measurement. Caller holds `_hw`."""
        be = self.backend
        return {"blade": be.get_blade(), "ref": be.get_ref(), "output": be.get_output(),
                "nh": be.get_nharmonic(), "dh": be.get_dharmonic(),
                "freq": be.get_frequency(), "phase": be.get_phase(),
                "enable": be.get_enable()}

    def _apply_read(self, st: dict) -> None:
        """Store a config read (caller holds `_lock`). Indices -> names via the
        blade table; an index the table does not know is a hardware error."""
        blade = blade_by_index(st["blade"])
        ref_i, out_i = int(st["ref"]), int(st["output"])
        if not 0 <= ref_i < len(blade.ref_modes):
            raise ValueError(f"controller reports ref={ref_i}, not valid for {blade.name}")
        if not 0 <= out_i < len(blade.output_modes):
            raise ValueError(f"controller reports output={out_i}, not valid for {blade.name}")
        self._blade = blade
        self._ref = blade.ref_modes[ref_i]
        self._output = blade.output_modes[out_i]
        self._nh, self._dh = int(st["nh"]), int(st["dh"])
        self._phase = float(st["phase"])
        if bool(st["enable"]) != self._enabled:
            # a front-panel start/stop: the lock has to be earned again
            self._enabled = bool(st["enable"])
            self._restart_lock()
        # A front-panel frequency change: adopt it (only on internal reference).
        # The read-back of OUR OWN command is not a front-panel change, and it
        # need not equal the setpoint exactly: the unit rounds to its
        # synthesiser step, and `freq?` may answer in whole Hz even on the
        # 0.1 Hz blade (the thorlabs_mc2000b package reads it as an integer,
        # # VERIFY). So anything within max(step, 1 Hz) is ours. Adopting it
        # would change `setpoint_frequency_Hz` under a scan that waits for the
        # EXACT value it asked for (scan-core's adopt check, tol 1e-6), and the
        # scan would time out.
        dev_f = float(st["freq"])
        same = max(self._blade.resolution_Hz, 1.0) + 1e-9
        # NOT while a sweep moves the setpoint: the unit then legitimately
        # lags it by a step, and "adopting" that would walk the sweep back.
        if (not self._blade.is_external(self._ref) and math.isfinite(self._freq_sp)
                and not self._sweep.running
                and abs(dev_f - self._freq_sp) > same):
            self._freq_sp = dev_f
            self._restart_lock()

    def _restart_lock(self) -> None:
        """A change that moves the wheel. Caller holds `_lock`: clearing the lock
        and bumping the generation happen in the same critical section as the
        change itself."""
        self._gen += 1
        self._locked = False
        self._band_since = None
        self._changed_at = self._clock()

    def poll_once(self) -> None:
        """One read of the controller and one lock decision."""
        with self._lock:
            gen0 = self._gen
            external = self._blade.is_external(self._ref)
            self._npoll += 1
            full = (self._npoll % self.FULL_READ_EVERY) == 0
        t0 = time.perf_counter()
        try:
            with self._hw:
                state = self._read_config_locked() if full else None
                enable = self.backend.get_enable()
                tw0 = time.time()
                refout = self.backend.read_refout_frequency()
                t_wall = 0.5 * (tw0 + time.time())
                inp = self.backend.read_input_frequency() if external else _NAN
        except Exception as exc:
            with self._lock:
                self._hw_error = str(exc) or type(exc).__name__
                self._locked = False
                self._band_since = None
            return
        now = self._clock()
        with self._lock:
            self._hw_error = ""
            self._poll_ms = (time.perf_counter() - t0) * 1e3
            if self._gen != gen0:
                # A command landed while we were reading. EVERYTHING we read may
                # predate it: the enable flag, the frequency, the modes. Applying
                # it would REVERT the command in the brain (e.g. a start read as
                # "standby", which bumps lock_gen past the number the `start`
                # reply gave a scan -- that wait could then never finish). Throw
                # the whole read away; the next poll is ~0.2 s later.
                return
            self._readings += 1
            if state is not None:
                try:
                    self._apply_read(state)
                except ValueError as exc:
                    self._hw_error = str(exc)
            if bool(enable) != self._enabled:
                self._enabled = bool(enable)
                self._restart_lock()
            self._refout = float(refout)
            self._input = float(inp)
            b, ref = self._blade, self._ref
            ring_ref = b.ring_of(ref)
            ring_out = b.output_ring(self._output, ref)
            if ring_out is None:
                self._measured = _NAN
            else:
                # REF OUT may follow the OTHER ring of a 10/100 blade: scale it
                # to the ring the reference locks to (same wheel, other slots)
                self._measured = self._refout * b.slots(ring_ref) / b.slots(ring_out)
            # into the stream, stamped at the middle of the read (wall clock:
            # the coordinator may sit on another PC)
            self.recorder.append(t_wall, (self._measured, self._refout))
            if self._gen != gen0:
                return          # _apply_read above adopted a front-panel change: judge it next poll
            if self._sweep.running:
                # the setpoint MOVES: there is nothing to be locked to yet
                self._locked = False
                self._band_since = None
                return
            target = self._target_locked()
            s = self.cfg.settle
            if ring_out is None:
                self._locked = (self._enabled and target > 0
                                and now - self._changed_at >= float(s.blind_lock_s))
                return
            tol = max(float(s.tolerance_Hz), float(s.tolerance_rel) * abs(target))
            ok = (self._enabled and target > 0 and math.isfinite(self._measured)
                  and abs(self._measured - target) <= tol)
            if not ok:
                self._band_since = None
                self._locked = False
            else:
                if self._band_since is None:
                    self._band_since = now
                self._locked = (now - self._band_since) >= float(s.hold_s)

    def _target_locked(self) -> float:
        """The frequency the wheel should chop at (caller holds `_lock`)."""
        if self._blade.is_external(self._ref):
            inp = self._input if math.isfinite(self._input) else 0.0
            return inp * self._nh / max(self._dh, 1)
        # The wheel can only run on the synthesiser's grid (1 Hz, 0.1 Hz on the
        # 10/100 blade): judge the lock against the grid point we actually
        # sent, not the unrounded request, or a tight tolerance could never be met.
        return self._quantize(self._freq_sp)

    def _quantize(self, hz: float) -> float:
        """`hz` on the mounted blade's frequency grid (caller holds `_lock`
        or accepts a blade read without it)."""
        if not math.isfinite(hz):
            return hz
        res = self._blade.resolution_Hz
        return round(round(hz / res) * res, 6)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.poll_once()
            hw = self.cfg.hardware
            fast = self._sweep.running or self.recorder.running
            hz = float(getattr(hw, "stream_poll_hz", hw.poll_hz) if fast else hw.poll_hz)
            dt = 1.0 / max(hz, 0.2)
            # deadline + short time.sleep slices, not Event.wait(timeout): on
            # Windows a timed wait sleeps at least one 15.6 ms tick (gotcha #34)
            end = t0 + dt
            while not self._stop.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.01))

    # ---- the frequency SWEEP (fly scans) -----------------------------------------

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float) -> int:
        """Sweep the chopping frequency to `hz` at `rate_Hz_per_s` (internal
        reference only); returns the sweep's number. The target is clamped to
        the live range (blade ring AND envelope) and the rate to
        limits.sweep_rate_*, each with a warning. It does not start the wheel:
        in standby only the setpoint walks."""
        self._require_connected()
        hz = _finite(hz, "frequency")
        rate = abs(_finite(rate_Hz_per_s, "rate"))
        if not rate > 0:
            raise ValueError("rate must be > 0")
        with self._lock:
            if self._blade.is_external(self._ref):
                raise ValueError("on external reference the frequency comes from EXT REF IN "
                                 "(times N/D); a sweep needs an internal reference")
            running = self._enabled
        lim = self.cfg.limits
        lo_r, hi_r = sorted((float(lim.sweep_rate_min_Hz_per_s),
                             float(lim.sweep_rate_max_Hz_per_s)))
        r, rclamped = _clamp(rate, lo_r, hi_r)
        lo, hi = self.freq_limits()
        value, clamped = _clamp(hz, lo, hi)
        self._sweep.stop()           # a new sweep replaces a running one, from where it is
        rid = self._sweep.start(value, r)
        if clamped or rclamped:
            self._emit("warn", f"sweep clamped to {value:g} Hz at {r:g} Hz/s "
                               f"(range {lo:g}..{hi:g} Hz, {lo_r:g}..{hi_r:g} Hz/s)")
        self._emit("info", f"frequency sweep #{rid} -> {value:g} Hz at {r:g} Hz/s"
                   + ("" if running else " (standby: only the setpoint walks)"))
        return rid

    def ramp_stop(self) -> bool:
        """End a sweep WHERE IT IS (a scan's Abort; a safety verb). True if
        one was running."""
        was = self._sweep.stop()
        if was:
            self._emit("info", f"frequency sweep stopped at {self._freq_sp:g} Hz")
        return was

    # the stream verbs: the poll thread's REF OUT readings, plus the sweep's
    # record of what it commanded (channel "commanded", its own stamps)
    def stream_start(self) -> int:
        self._sweep.stream_start()
        return self.recorder.start()

    def stream_read(self) -> dict:
        return self._merge(self.recorder.read(), self._sweep.stream_read())

    def stream_stop(self) -> dict:
        return self._merge(self.recorder.stop(), self._sweep.stream_stop())

    @staticmethod
    def _merge(chunk: dict, sweep: dict) -> dict:
        """The poll's chunk with the sweep's commanded values added as a
        channel of their own, on their own time stamps (`t_ch`, guide 6b)."""
        chunk["values"]["commanded"] = sweep["values"]["commanded"]
        chunk.setdefault("t_ch", {})["commanded"] = sweep["t"]
        chunk["delay_s"]["commanded"] = 0.0
        chunk["overflow"] = bool(chunk["overflow"] or sweep["overflow"])
        return chunk

    def _stop_sweep(self, why: str) -> None:
        """Stop a running sweep because `why` takes the frequency over.
        Called WITHOUT a lock held (the step in progress may need one)."""
        if self._sweep.stop():
            self._emit("info", f"frequency sweep stopped by {why} at {self._freq_sp:g} Hz")

    def _sweep_step(self, hz: float) -> None:
        """One step, on the sweep's thread: the synthesiser gets the value on
        its grid -- written only when that grid value changes (1 Hz grid at a
        few Hz/s: most steps change nothing and send nothing) -- and the
        setpoint follows exactly. NOT a _restart_lock() per step: bumping the
        generation would make the poll throw away every reading taken during
        the sweep (see poll_once), and the sweep needs exactly those. The poll
        keeps `locked` False while a sweep runs instead."""
        with self._lock:
            if self._blade.is_external(self._ref):
                raise RuntimeError("the reference became external")
            sent = self._quantize(float(hz))
        if sent != self._sent_hz:
            with self._hw:
                self.backend.set_frequency(sent)
            self._sent_hz = sent
        with self._lock:
            self._freq_sp = float(hz)
            self._locked = False
            self._band_since = None
            self._changed_at = self._clock()

    def _sweep_done(self, rid: int, reason: str) -> None:
        with self._lock:
            # judge the lock afresh at where the sweep ended (a new generation:
            # a reading taken before this moment must not count)
            self._restart_lock()
            here = self._freq_sp
        self._sent_hz = _NAN
        if reason == "done":
            self._emit("info", f"frequency sweep #{rid} done at {here:g} Hz")
        elif reason.startswith("error"):
            self._emit("error", f"frequency sweep #{rid} ended: {reason}")

    # ---- limits --------------------------------------------------------------

    def freq_limits(self) -> tuple[float, float]:
        """The live frequency range: the referenced ring of the mounted blade,
        intersected with the safety envelope. Changes with blade and ref mode."""
        with self._lock:
            b, ref = self._blade, self._ref
        lo, hi = b.range_Hz(ref)
        lim = self.cfg.limits
        lo, hi = max(lo, float(lim.freq_min_Hz)), min(hi, float(lim.freq_max_Hz))
        if lo > hi:                          # envelope excludes the blade entirely
            lo = hi = max(min(lo, hi), 0.0)
        return lo, hi

    def blade_options(self) -> list[str]:
        """Blades the control may offer: the owned ones plus the mounted one."""
        owned = parse_owned(self.cfg.blades.owned)
        with self._lock:
            cur = self._blade.name
        return owned + ([cur] if cur not in owned else [])

    def mode_options(self) -> tuple[tuple, tuple]:
        with self._lock:
            return self._blade.ref_modes, self._blade.output_modes

    # ---- commands --------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("chopper is not connected")

    def _require_standby(self, what: str) -> None:
        with self._lock:
            running = self._enabled
        if running:
            raise ValueError(f"{what} can only be changed in standby -- disable the "
                             f"chopper first (MC2000B manual 5.2)")

    def set_frequency(self, hz: float) -> float:
        """Internal-reference chopping frequency in Hz, clamped to the live range.
        Returns the value actually set. Refused on external reference."""
        self._require_connected()
        hz = _finite(hz, "frequency")
        with self._lock:
            if self._blade.is_external(self._ref):
                raise ValueError("on external reference the frequency comes from EXT REF IN "
                                 "(times N/D); switch to an internal reference to set it")
        # a set takes the frequency over from a sweep (stopped BEFORE the locks
        # a sweep step may be waiting for)
        self._stop_sweep("a frequency set")
        lo, hi = self.freq_limits()
        value, clamped = _clamp(hz, lo, hi)
        # Send the value on the synthesiser grid; KEEP the setpoint exactly as
        # requested (after clamping), because a scan recognises "my point was
        # adopted" by comparing the status setpoint with what it asked for.
        with self._lock:
            sent = self._quantize(value)
        with self._hw:
            self.backend.set_frequency(sent)
        with self._lock:
            self._freq_sp = value
            self._restart_lock()
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz (allowed {lo:g}..{hi:g} Hz "
                               f"for {self._blade.name}, {self._ref})")
        else:
            self._emit("info", f"frequency = {value:g} Hz")
        return value

    def set_phase(self, deg: float) -> float:
        self._require_connected()
        deg = _finite(deg, "phase")
        lim = self.cfg.limits
        value, clamped = _clamp(deg, float(lim.phase_min_deg), float(lim.phase_max_deg))
        with self._hw:
            self.backend.set_phase(value)
        with self._lock:
            self._phase = value
            # the PLL slews the wheel to the new phase: that is a re-lock too
            self._restart_lock()
        if clamped:
            self._emit("warn", f"phase clamped to {value:g} deg "
                               f"(limit {lim.phase_min_deg:g}..{lim.phase_max_deg:g})")
        else:
            self._emit("info", f"phase = {value:g} deg")
        return value

    def set_enable(self, on: bool) -> int:
        """Run (True) or standby (False). Returns the lock generation of this
        command: status `lock_gen` equal to it plus `locked` = THIS start locked."""
        self._require_connected()
        on = bool(on)
        if not on:
            self._stop_sweep("standby")
        with self._hw:
            self.backend.set_enable(on)
        with self._lock:
            self._enabled = on
            self._restart_lock()
            gen = self._gen
        self._emit("info", "chopper RUNNING" if on else "chopper in STANDBY")
        return gen

    def standby(self) -> int:
        """Stop the wheel (standby). Same as set_enable(False); a name of its
        own because over the wire it is the SAFETY verb `stop` a viewer may
        always send (net/service.py, control), and the GUI's Stop button calls
        it on a local brain and a remote client alike."""
        return self.set_enable(False)

    def set_blade(self, name: str) -> None:
        self._require_connected()
        blade = blade_by_name(name)
        if blade.name not in self.blade_options():
            raise ValueError(f"blade {blade.name} is not in blades.owned "
                             f"({self.cfg.blades.owned}); add it in Settings first")
        self._require_standby("the blade")
        self._stop_sweep("a blade change")
        with self._lock:
            old_ref, old_out = self._ref, self._output
        # The ref / output INDICES mean different things on a different blade
        # (ref=1 is "int-inner" on the 10/100 blade but "external" on the
        # 60-slot one). Carry the MEANING over instead of the number.
        want_ref = _equivalent_ref(old_ref, blade)
        want_out = _equivalent_output(old_out, want_ref, blade)
        with self._hw:
            self.backend.set_blade(blade.index)
            state = self._read_config_locked()   # modes may have been reset by the unit
            if blade.ref_modes[int(state["ref"]) % len(blade.ref_modes)] != want_ref:
                self.backend.set_ref(blade.ref_modes.index(want_ref))
            if blade.output_modes[int(state["output"]) % len(blade.output_modes)] != want_out:
                self.backend.set_output(blade.output_modes.index(want_out))
            state = self._read_config_locked()
        with self._lock:
            self._apply_read(state)
            self._restart_lock()
        self._emit("info", f"blade = {blade.name} (ref {self._ref}, output {self._output})")
        self._reclamp_frequency()

    def set_ref_mode(self, mode: str) -> None:
        self._require_connected()
        mode = str(mode).strip().lower()
        refs, _ = self.mode_options()
        if mode not in refs:
            raise ValueError(f"reference mode {mode!r} does not exist for "
                             f"{self._blade.name}; choose one of {', '.join(refs)}")
        self._require_standby("the reference mode")
        self._stop_sweep("a reference-mode change")
        with self._hw:
            self.backend.set_ref(refs.index(mode))
        with self._lock:
            self._ref = mode
            self._restart_lock()
        self._emit("info", f"reference in = {mode}")
        self._reclamp_frequency()

    def set_output_mode(self, mode: str) -> None:
        self._require_connected()
        mode = str(mode).strip().lower()
        _, outs = self.mode_options()
        if mode not in outs:
            raise ValueError(f"output mode {mode!r} does not exist for "
                             f"{self._blade.name}; choose one of {', '.join(outs)}")
        self._require_standby("the reference output")
        with self._hw:
            self.backend.set_output(outs.index(mode))
        with self._lock:
            self._output = mode
            # the old reading came from a different signal: drop it now rather
            # than show it under the new mode until the next poll
            self._measured = _NAN
            self._restart_lock()
        self._emit("info", f"reference out = {mode}"
                   + ("  (wheel not observable: lock by timer)" if mode == "target" else ""))

    def set_harmonics(self, n: int | None = None, d: int | None = None) -> None:
        """External-reference multiplier N and divider D (each 1..15): the wheel
        locks to EXT REF IN x N / D."""
        self._require_connected()
        self._require_standby("the harmonics")
        vals = {}
        for key, v in (("n", n), ("d", d)):
            if v is None:
                continue
            iv = int(round(_finite(v, key)))
            c, clamped = _clamp(iv, 1, 15)
            if clamped:
                self._emit("warn", f"harmonic {key.upper()} clamped to {c} (1..15)")
            vals[key] = int(c)
        with self._hw:
            if "n" in vals:
                self.backend.set_nharmonic(vals["n"])
            if "d" in vals:
                self.backend.set_dharmonic(vals["d"])
        with self._lock:
            self._nh = vals.get("n", self._nh)
            self._dh = vals.get("d", self._dh)
            self._restart_lock()
            nh, dh = self._nh, self._dh
        self._emit("info", f"harmonics N/D = {nh}/{dh}")

    def _reclamp_frequency(self) -> None:
        """After a blade / ref / limits change the old setpoint may be outside
        the new range: move it inside, loudly."""
        with self._lock:
            ext = self._blade.is_external(self._ref)
            f = self._freq_sp
        if ext or not math.isfinite(f):
            return
        lo, hi = self.freq_limits()
        if not lo <= f <= hi:
            self.set_frequency(f)            # clamps and warns

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot of what the poll thread last stored. Never touches hardware."""
        lo, hi = self.freq_limits()
        owned = parse_owned(self.cfg.blades.owned)
        r = self._sweep.status()             # in memory: the sweep's live state
        with self._lock:
            target = self._target_locked()
            err = (self._measured - target) if math.isfinite(self._measured) else _NAN
            return Status(
                connected=self._connected, simulated=self._simulated, idn=self._idn,
                hw_error=self._hw_error, blade=self._blade.name, ref_mode=self._ref,
                output_mode=self._output, external=self._blade.is_external(self._ref),
                enabled=self._enabled, setpoint_frequency_Hz=self._freq_sp,
                target_frequency_Hz=target, frequency_Hz=self._measured,
                freq_error_Hz=err, refout_frequency_Hz=self._refout,
                input_frequency_Hz=self._input, locked=self._locked,
                lock_source=(LOCK_TIMER if self._blade.output_ring(self._output, self._ref)
                             is None else LOCK_MEASURED),
                lock_gen=self._gen,
                phase_deg=self._phase, nharmonic=self._nh, dharmonic=self._dh,
                freq_min_Hz=lo, freq_max_Hz=hi, owned_blades=owned,
                readings=self._readings, poll_ms=self._poll_ms,
                ramping=bool(r["ramping"]), ramp_id=int(r["ramp_id"]),
                ramp_target_Hz=_NAN if r["ramp_target"] is None else float(r["ramp_target"]),
                ramp_rate_Hz_per_s=_NAN if r["ramp_rate"] is None else float(r["ramp_rate"]))

    # ---- settings (Settings dialog / wire use these) ------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config edited self.cfg in place: the safety envelope
        may have moved, so bring the setpoints back inside it."""
        if not self._connected:
            return
        self._reclamp_frequency()
        lim = self.cfg.limits
        with self._lock:
            ph = self._phase
        if not float(lim.phase_min_deg) <= ph <= float(lim.phase_max_deg):
            self.set_phase(ph)

    # ---- internals -------------------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
