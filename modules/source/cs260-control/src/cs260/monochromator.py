"""The Monochromator: the brain between the wire and the backend.

A monochromator is set-and-forget in what it HOLDS, but its moves take time:
the grating turret slews at ~200 nm/s, a grating swap takes seconds, the filter
wheel about one. So the brain's real job is to say honestly when a requested
wavelength has been REACHED -- wavelength is THE scan axis of this instrument,
and a scan that trusted a reply ("accepted") instead of the arrival would
record every point at the previous wavelength.

Its jobs:
  * clamp every request to the envelope (the current grating's range, fenced
    by `limits`), and announce a clamp as a warn event;
  * SEQUENCE multi-step operations: a grating change is "close shutter ->
    swap grating -> go back to the wavelength you had -> re-open shutter";
    a wavelength move with order sorting is "move -> move the filter wheel";
  * publish `target_nm` together with `moving`, so a scan waits for
    "the service has ADOPTED my target AND nothing is moving"
    (the adopt_then_flag rule, docs/DEVELOPER_NOTES.md gotcha #2).

Threads and locks (the rules the other modules learned the hard way):
  * ONE worker thread reads the hardware and starts queued steps. `status()`
    only copies what that thread stored and never touches the instrument, so a
    slow GPIB query cannot stall the status publisher (gotcha #1).
  * EVERY backend call runs under `_hw` (an RLock): the command thread and the
    worker would otherwise talk on the GPIB bus at once.
  * A setter stores its new target, sets `moving` and bumps a command
    GENERATION in the SAME critical section (gotcha #1, #28). So no status frame
    can show the new target next to the old point's `moving = False`, and a
    read that started before the command cannot declare the new move finished.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from .backends.base import MonochromatorBackend, MonoState
from .config import Config, parse_filter_bands, parse_labels

_NAN = float("nan")

#: ERROR? codes (Cornerstone manual 16.7), for readable events.
ERROR_TEXT = {
    0: "system error",
    1: "command not understood",
    2: "bad parameter",
    3: "destination position not allowed",
    6: "accessory not present",
    7: "accessory already in that position",
    8: "could not home the wavelength drive",
    9: "label too long",
}


@dataclass
class Status:
    """One snapshot of the monochromator, for status() and the wire."""

    connected: bool
    simulated: bool = True
    idn: str = ""
    hw_error: str = ""
    # wavelength
    wavelength_nm: float = _NAN      # where the drive IS (WAVE?)
    target_nm: float = _NAN          # where we asked it to go (adopt key)
    moving: bool = False             # anything mechanical still in progress
    busy: str = ""                   # what: wavelength / grating / filter / port / step
    wl_min_nm: float = _NAN          # LIVE envelope of the target grating
    wl_max_nm: float = _NAN
    # grating
    grating: int = 0
    grating_target: int = 0
    grating_lines: int = 0
    grating_label: str = ""
    n_gratings: int = 0
    bandpass_nm: float = _NAN
    # accessories
    shutter_open: bool = False
    filter: int = 0
    filter_target: int = 0
    filter_label: str = ""
    filter_fitted: bool = False
    port: int = 1
    port_target: int = 1
    port_fitted: bool = False
    # housekeeping
    step_position: int = 0
    error_code: int = -1             # last instrument error, -1 = none since start
    error_text: str = ""
    moves: int = 0
    readings: int = 0
    poll_ms: float = _NAN


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


class Monochromator:
    def __init__(self, backend: MonochromatorBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards targets, readings, flags, queue
        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._info: dict[int, tuple[int, str]] = {}   # grating -> (lines, label) from the box
        # what we ask for (under _lock)
        self._target_nm = _NAN
        self._grating_target = 1
        self._filter_target = 0
        self._port_target = 1
        self._queue: deque = deque()        # steps still to start: (kind, arg)
        # URGENT requests (shutter, abort) that must not wait behind a move.
        # The command thread only files them here; the worker sends them at the
        # start of its next poll. Why not send them straight from the command
        # thread: on the real instrument a query sent during a slew may only be
        # answered when the slew is over (see backends/cornerstone.py), so the
        # worker can hold the GPIB lock for seconds -- and a command thread
        # waiting on that lock would leave the client's request unanswered until
        # it timed out. A reply means ACCEPTED; the status shows the effect.
        self._urgent: deque = deque()
        self._busy = False
        self._busy_what = ""
        self._gen = 0                       # bumped by every command
        # what the worker last read (under _lock)
        self._st = MonoState(wavelength_nm=_NAN, grating=0, shutter_open=False)
        self._error_code = -1
        self._moves = 0
        self._readings = 0
        self._poll_ms = _NAN
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- the envelope ------------------------------------------------------------

    def limits_for(self, grating: int) -> tuple[float, float]:
        """The wavelength range grating `grating` may be sent to: its own range
        intersected with the absolute `limits` envelope."""
        _, _, lo, hi = self.cfg.gratings.of(grating)
        lim = self.cfg.limits
        lo, hi = max(lo, lim.wavelength_min_nm), min(hi, lim.wavelength_max_nm)
        return (lo, max(lo, hi))

    def live_limits(self) -> tuple[float, float]:
        """Range for the grating we are ON or GOING TO (a scan that swaps
        grating and then sets a wavelength must be clamped to the new one)."""
        with self._lock:
            g = self._grating_target
        return self.limits_for(g)

    def _n_gratings(self) -> int:
        return max(1, min(3, int(self.cfg.gratings.count)))

    # ---- lifecycle -----------------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Connect, READ where everything is and ADOPT it as the target, then
        start the worker. `poll=False` is for tests that step `poll_once()`.

        Lukas's rule (2026-09-27, every module): starting the service must not
        change the instrument. So nothing is written here -- no move, no shutter,
        no filter, no config push. Wavelength, grating, shutter, filter wheel and
        exit port are whatever the box says; the GUI and describe (whose
        wavelength envelope follows the adopted grating) show that state. A
        setting only reaches the instrument when someone asks for it."""
        with self._hw:
            self.backend.open()             # queries only (see backends/cornerstone.py)
            self._idn = self.backend.idn()
            for n in range(1, self._n_gratings() + 1):
                got = self.backend.grating_info(n)
                if got:
                    self._info[n] = got
            st = self.backend.read_state()
            notes = list(getattr(self.backend, "startup_notes", []) or [])
        with self._lock:
            self._st = st
            self._target_nm = st.wavelength_nm
            self._grating_target = st.grating or 1
            self._filter_target = st.filter
            self._port_target = st.port or 1
            self._connected = True
        self._emit("info", f"connected: {self._idn or 'Cornerstone 260'}; found it at "
                           f"{st.wavelength_nm:.3f} nm on grating {st.grating}, shutter "
                           f"{'open' if st.shutter_open else 'closed'} (nothing changed)")
        # e.g. "the box works in um; converted in software" -- worth seeing once
        for msg in notes:
            self._emit("warn", msg)
        self.poll_once()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="cs260-worker",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop the worker, optionally close the shutter, disconnect. The drive
        is left where it is. Safe to call more than once."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        with self._lock:
            was = self._connected
            self._queue.clear()
        if was:
            # an ABORT (or shutter request) filed just before the stop is still
            # sent: a drive told to stop must not keep slewing because we quit
            self._run_urgent()
        try:
            with self._hw:
                if was and self.cfg.shutter.close_on_shutdown:
                    try:
                        self.backend.set_shutter(False)
                    except Exception as exc:        # still disconnect
                        self._emit("error", f"could not close the shutter: {exc}")
                self.backend.close()
        finally:
            with self._lock:
                self._connected = False
                self._busy = False
            if was:
                self._emit("info", "disconnected" + (" (shutter closed)"
                                   if self.cfg.shutter.close_on_shutdown else ""))

    # ---- commands --------------------------------------------------------------------

    def set_wavelength(self, nm: float) -> float:
        """Go to `nm` (clamped to the live envelope). Returns the accepted value."""
        lo, hi = self.live_limits()
        value, clamped = _clamp(_finite(nm, "wavelength_nm"), lo, hi)
        with self._lock:
            self._require_connected()
            self._target_nm = value
            # a newer wavelength replaces any not-yet-started one
            self._queue = deque(s for s in self._queue if s[0] not in ("wave", "autofilter"))
            self._queue.append(("wave", value))
            if self.cfg.accessories.filter_wheel and self.cfg.accessories.auto_filter:
                self._queue.append(("autofilter", value))
            self._mark_busy("wavelength")
        if clamped:
            self._emit("warn", f"wavelength clamped to {value:g} nm "
                               f"(grating range {lo:g}..{hi:g} nm)")
        else:
            self._emit("info", f"wavelength -> {value:g} nm")
        return value

    def set_grating(self, n: int) -> int:
        """Swap to grating `n`. Unless configured otherwise: close the shutter
        for the swap, go back to the wavelength you had (clamped to the new
        grating's range), re-open the shutter."""
        n = int(n)
        count = self._n_gratings()
        if not 1 <= n <= count:
            raise ValueError(f"grating must be 1..{count}, got {n}")
        lo, hi = self.limits_for(n)
        restore = bool(self.cfg.motion.restore_wavelength_after_grating)
        with self._lock:
            self._require_connected()
            already = n == self._grating_target and not self._queue
        if already:
            self._emit("info", f"already on grating {n}")
            return n
        with self._lock:
            want = self._target_nm if math.isfinite(self._target_nm) else self._st.wavelength_nm
            back, clamped = _clamp(want, lo, hi)
            was_open = bool(self._st.shutter_open)
            close = bool(self.cfg.shutter.close_during_grating_change) and was_open
            self._grating_target = n
            self._queue = deque(s for s in self._queue if s[0] not in ("wave", "grating"))
            if close:
                self._queue.append(("shutter", False))
            self._queue.append(("grating", n))
            if restore:
                self._queue.append(("wave", back))
                self._target_nm = back
            else:
                self._target_nm = _NAN          # the drive parks where the box decides
            if close:
                self._queue.append(("shutter", True))
            self._mark_busy("grating")
        msg = f"grating -> {n}"
        if restore:
            msg += f", then back to {back:g} nm" + (" (clamped to its range)" if clamped else "")
        self._emit("warn" if (restore and clamped) else "info", msg)
        return n

    def set_shutter(self, open_: bool) -> None:
        """Open/close at the NEXT POLL -- it jumps the move queue (closing the
        shutter during a slow move is exactly when you want it). It also cancels
        a pending 're-open after the grating change', so it cannot be
        overridden. Settle on `shutter_open` echoing the request."""
        open_ = bool(open_)
        with self._lock:
            self._require_connected()
            self._queue = deque(s for s in self._queue if s[0] != "shutter")
            self._urgent = deque(u for u in self._urgent if u[0] != "shutter")
            self._urgent.append(("shutter", open_))
        self._emit("info", f"shutter -> {'OPEN' if open_ else 'CLOSED'}")

    def close_shutter(self) -> None:
        """Close the shutter. Same as set_shutter(False); a name of its own
        because over the wire it is a SAFETY verb a viewer may always send
        (net/service.py, control) -- set_shutter can also OPEN it."""
        self.set_shutter(False)

    def set_filter(self, n: int) -> int:
        acc = self.cfg.accessories
        if not acc.filter_wheel:
            raise ValueError("no filter wheel fitted (accessories.filter_wheel = false)")
        count = max(1, min(6, int(acc.filter_count)))
        n = int(n)
        if not 1 <= n <= count:
            raise ValueError(f"filter must be 1..{count}, got {n}")
        with self._lock:
            self._require_connected()
            self._filter_target = n
            self._queue = deque(s for s in self._queue if s[0] not in ("filter", "autofilter"))
            self._queue.append(("filter", n))
            self._mark_busy("filter")
        self._emit("info", f"filter -> {n} ({self._filter_label(n)})")
        return n

    def set_port(self, n: int) -> int:
        if not self.cfg.accessories.dual_port:
            raise ValueError("single exit port (accessories.dual_port = false)")
        n = int(n)
        if n not in (1, 2):
            raise ValueError(f"port must be 1 (axial) or 2 (lateral), got {n}")
        with self._lock:
            self._require_connected()
            self._port_target = n
            self._queue = deque(s for s in self._queue if s[0] != "port")
            self._queue.append(("port", n))
            self._mark_busy("port")
        self._emit("info", f"exit port -> {n} ({self._port_label(n)})")
        return n

    def step(self, steps: int) -> None:
        """Nudge the drive by motor steps. The wavelength it lands on is then
        adopted as the target, since nobody asked for a particular one."""
        steps = int(steps)
        with self._lock:
            self._require_connected()
            self._queue.append(("step", steps))
            self._target_nm = _NAN              # re-adopted when the step is over
            self._mark_busy("step")
        self._emit("info", f"step {steps:+d}")

    def abort(self) -> None:
        """Stop the wavelength drive, drop everything queued. The position it
        stopped at becomes the target, so nothing restarts on its own."""
        with self._lock:
            self._require_connected()
            self._queue.clear()
            self._target_nm = _NAN              # re-adopted where the drive stopped
            self._gen += 1
            # first in line: sent before anything else at the next poll
            self._urgent = deque(u for u in self._urgent if u[0] != "abort")
            self._urgent.appendleft(("abort", None))
        self._emit("warn", "ABORT: stopping the drive, queue cleared")

    def calibrate(self, nm: float) -> None:
        """Declare the CURRENT position to be `nm`. Rewrites the grating offset
        stored in the instrument -- only after checking a known line."""
        nm = _finite(nm, "wavelength_nm")
        with self._lock:
            self._require_connected()
            if self._busy:
                raise ValueError("cannot calibrate while moving")
        with self._hw:
            self.backend.calibrate(nm)
        with self._lock:
            self._target_nm = nm
            self._gen += 1
        self._emit("warn", f"CALIBRATE: current position is now {nm:g} nm")

    # ---- status ------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot of what the worker last read. Never touches the instrument."""
        acc = self.cfg.accessories
        with self._lock:
            st = self._st
            g = st.grating or self._grating_target
            lines, label = self._grating_lines_label(g)
            lo, hi = self.limits_for(self._grating_target)
            err = self._error_code
            return Status(
                connected=self._connected,
                simulated=bool(getattr(self.backend, "simulated", True)),
                idn=self._idn,
                hw_error=self._hw_error,
                wavelength_nm=st.wavelength_nm,
                target_nm=self._target_nm,
                moving=self._busy,
                busy=self._busy_what if self._busy else "",
                wl_min_nm=lo,
                wl_max_nm=hi,
                grating=st.grating,
                grating_target=self._grating_target,
                grating_lines=lines,
                grating_label=label,
                n_gratings=self._n_gratings(),
                bandpass_nm=self._bandpass(lines),
                shutter_open=bool(st.shutter_open),
                filter=st.filter,
                filter_target=self._filter_target,
                filter_label=self._filter_label(st.filter) if st.filter else "",
                filter_fitted=bool(acc.filter_wheel),
                port=st.port,
                port_target=self._port_target,
                port_fitted=bool(acc.dual_port),
                step_position=st.step_position,
                error_code=err,
                error_text=ERROR_TEXT.get(err, "") if err >= 0 else "",
                moves=self._moves,
                readings=self._readings,
                poll_ms=self._poll_ms,
            )

    # ---- settings -------------------------------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-check the settings after set_config edited self.cfg in place.
        Nothing is MOVED by a settings change: a target outside a new envelope
        is announced, not silently re-driven."""
        g = self.cfg.gratings
        g.count = max(1, min(3, int(g.count)))
        acc = self.cfg.accessories
        acc.filter_count = max(1, min(6, int(acc.filter_count)))
        try:
            parse_filter_bands(acc.filter_bands)
        except ValueError as exc:
            acc.auto_filter = False
            self._emit("error", f"filter_bands not understood ({exc}); auto filter OFF")
        # The real backend polls FILTER?/OUTPORT? only for fitted accessories
        # (asking an absent wheel sets an instrument error), so tell it.
        hook = getattr(self.backend, "configure_accessories", None)
        if hook is not None:
            with self._hw:
                hook(bool(acc.filter_wheel), bool(acc.dual_port))
        lo, hi = self.live_limits()
        with self._lock:
            t = self._target_nm
        if math.isfinite(t) and not lo <= t <= hi:
            self._emit("warn", f"wavelength target {t:g} nm is outside the new range "
                               f"{lo:g}..{hi:g} nm (not moved)")

    # ---- the worker -------------------------------------------------------------------------

    def _worker(self) -> None:
        # Deadline scheduling with time.sleep, not Event.wait: on Windows a timed
        # wait sleeps at least a 15.6 ms tick (gotcha #34).
        next_t = self._clock()
        while not self._stop.is_set():
            self.poll_once()
            next_t += max(0.02, float(self.cfg.motion.poll_s))
            delay = next_t - self._clock()
            if delay < 0:
                next_t = self._clock()
            else:
                end = time.monotonic() + delay
                while not self._stop.is_set() and time.monotonic() < end:
                    time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def poll_once(self) -> None:
        """Read the instrument once, update the flags, start the next step."""
        self._run_urgent()
        with self._lock:
            gen = self._gen
        t0 = self._clock()
        try:
            with self._hw:
                st = self.backend.read_state()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            with self._lock:
                new = msg != self._hw_error
                self._hw_error = msg
            if new:
                self._emit("error", f"instrument read failed: {msg}")
            return
        events = []
        start = None
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._st = st
            self._readings += 1
            self._poll_ms = (self._clock() - t0) * 1000.0
            if st.error_code is not None:
                self._error_code = int(st.error_code)
                events.append(("error", f"instrument error {st.error_code}: "
                               f"{ERROR_TEXT.get(st.error_code, 'unknown')}"))
                if self._busy and st.error_code not in (7,):
                    # The move will not happen. Do NOT let `moving` fall with the
                    # target still set -- a scan would take that as arrival and
                    # measure at the wrong wavelength. Un-adopt the target so the
                    # wait times out loudly instead.
                    self._queue.clear()
                    self._target_nm = _NAN
                    self._busy = False
            if gen == self._gen and not st.moving:
                if self._queue:
                    start = self._queue.popleft()
                elif self._busy:
                    self._busy = False
                    # a step or an unrestored grating swap: adopt where it landed
                    if not math.isfinite(self._target_nm):
                        self._target_nm = st.wavelength_nm
                    events.append(("info", f"arrived: {st.wavelength_nm:.3f} nm, "
                                           f"grating {st.grating}"))
        if recovered:
            self._emit("info", "instrument readings recovered")
        for lvl, msg in events:
            self._emit(lvl, msg)
        if start is not None:
            self._run_step(start, st)

    def _run_urgent(self) -> None:
        """Send the filed shutter / abort requests (worker thread only)."""
        with self._lock:
            todo, self._urgent = list(self._urgent), deque()
        for kind, arg in todo:
            try:
                with self._hw:
                    if kind == "abort":
                        self.backend.abort()
                    elif kind == "shutter":
                        self.backend.set_shutter(bool(arg))
            except Exception as exc:
                self._emit("error", f"{kind} failed: {type(exc).__name__}: {exc}")

    def _run_step(self, step, st: MonoState) -> None:
        kind, arg = step
        try:
            with self._hw:
                if kind == "wave":
                    self.backend.goto(float(arg))
                elif kind == "grating":
                    if st.grating != int(arg):    # avoid error 7 'already there'
                        self.backend.set_grating(int(arg))
                elif kind == "filter":
                    if st.filter != int(arg):     # avoid error 7 'already there'
                        self.backend.set_filter(int(arg))
                elif kind == "autofilter":
                    pos = self._band_filter(float(arg))
                    if pos and pos != st.filter:
                        with self._lock:
                            self._filter_target = pos
                        self.backend.set_filter(pos)
                        self._emit("info", f"order sorting: filter -> {pos} "
                                           f"({self._filter_label(pos)})")
                elif kind == "port":
                    if st.port != int(arg):
                        self.backend.set_port(int(arg))
                elif kind == "step":
                    self.backend.step(int(arg))
                elif kind == "shutter":
                    self.backend.set_shutter(bool(arg))
            with self._lock:
                if kind in ("wave", "grating", "filter", "port", "step"):
                    self._moves += 1
                if kind in ("wave", "grating", "filter", "port", "step", "autofilter"):
                    self._busy_what = {"wave": "wavelength",
                                       "autofilter": "filter"}.get(kind, kind)
        except Exception as exc:
            with self._lock:
                self._queue.clear()
                self._target_nm = _NAN          # see poll_once: fail loudly
                self._busy = False
            self._emit("error", f"{kind} failed: {type(exc).__name__}: {exc}")

    # ---- internals ----------------------------------------------------------------------------

    def _mark_busy(self, what: str) -> None:
        """Call with _lock held: new command -> moving, new generation."""
        self._busy = True
        self._busy_what = what
        self._gen += 1

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("not connected")

    def _grating_lines_label(self, g: int) -> tuple[int, str]:
        if not g:
            return 0, ""
        if g in self._info:
            return self._info[g]
        lines, label, _, _ = self.cfg.gratings.of(g)
        return lines, label

    def _bandpass(self, lines: int) -> float:
        o = self.cfg.optics
        if not lines:
            return _NAN
        return (float(o.dispersion_nm_per_mm_at_1200) * 1200.0 / lines
                * float(o.slit_width_um) / 1000.0)

    def _filter_label(self, n: int) -> str:
        acc = self.cfg.accessories
        labels = parse_labels(acc.filter_labels, 6)
        return labels[n - 1] if 1 <= n <= 6 else ""

    def _port_label(self, n: int) -> str:
        return parse_labels(self.cfg.accessories.port_labels, 2)[n - 1]

    def _band_filter(self, nm: float) -> int:
        try:
            for pos, lo, hi in parse_filter_bands(self.cfg.accessories.filter_bands):
                if lo <= nm < hi:
                    return pos
        except ValueError:
            pass
        return 0

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
