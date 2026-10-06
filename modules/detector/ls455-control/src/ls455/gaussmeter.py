"""The Gaussmeter: the brain between the wire and the backend.

Its SETTINGS are set-and-forget (mode, resolution, range, display unit,
relative setpoint): clamp, push, read back, report. Its READING needs more
care, for the same reason as the suite's other acquire-style detectors: a scan
must never record a value that was measured before the scan step it is filed
under.

Two ways to read it:

  live      the latest reading from the polling thread. Right for a front
            panel -- and for a VNA that wants to know the field while it
            sweeps (status key `measured_field_mT`). Not guaranteed fresh for
            a scan point.

  acquire   the scan-safe read. `acquire()` returns an id at once (the suite's
            fire-and-forget contract). The polling thread then averages the
            next `acquisition.readings` readings that STARTED at least
            `settle_s` after the trigger, and latches mean and standard
            deviation as `sample`. `settle_s` is the meter's own DC filter
            catching up with a field step (the manual's time constant is 1 s
            at 5 digits, 0.1 s at 4: after 7 of them a step has settled to
            0.1 %, i.e. 7 s / 0.7 s). Callers wait until
            status shows `acq_id` == their id AND `acquiring` False -- the id
            check stops a stale "not acquiring" frame from the previous point
            fooling them (suite gotcha #17).

Start-up READS, it never writes (Lukas's rule, 2026-09-27, for every module):
connecting asks the meter for its probe, mode (DC / RMS / PEAK), resolution,
range, unit and relative setting and ADOPTS them into cfg and the status, so
the GUI and describe show what the meter is actually doing and a measurement
set up on the front panel is never changed by starting the service. The config
values are then only defaults that a user applies explicitly (a setter, or
set_config / Settings > Apply). There is no push-on-start option any more.

The front panel stays in charge WHILE the service runs, too. Readings arrive
in the display unit and are converted to mT in software, and RDGFIELD? means
nothing in peak mode, so a unit or mode changed by hand on the meter would
otherwise silently scale every later number (G -> T is a factor 10^4). The
polling thread therefore re-reads unit, mode, peak settings and range mode
every FRONT_PANEL_SYNC_S seconds, and once before the first reading of every
acquisition (queries only, ~0.25 s), adopts any change, logs it, and flags an
acquisition that a change ran into ("settings changed").

Threads and locks (the suite's rules for a polled detector):

  * ONE polling thread owns the readings. `status()` only copies what that
    thread stored and never touches the hardware, so a slow GPIB/serial call
    cannot stall the status publisher, and a dead link shows as `hw_error`
    instead of a healthy-looking panel full of old numbers.
  * EVERY backend call runs under `_hw` (an RLock), because the command thread
    and the polling thread would otherwise talk to the meter at once.
  * Setters change cfg / brain attributes; the snapshot is BUILT from them in
    status(), never edited in place (gotcha #1).
  * An acquisition's result and its "done" become visible in ONE critical
    section (gotcha #28).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import (DC_BANDWIDTH_HZ, DC_DIGITS, DC_TIME_CONSTANT_S,
                            FLAG_NO_PROBE, FLAG_OK, MODES, PEAK_TIME_CONSTANT_S,
                            PROBE_GEOMETRIES, RMS_BANDS, RMS_TIME_CONSTANT_S,
                            UNIT_CODES, GaussmeterBackend)
from .config import Config

_NAN = float("nan")

#: How often the polling thread re-reads the front-panel settings (seconds).
#: Four or five queries at the 455's 50 ms spacing, so ~5 % of the bus time.
FRONT_PANEL_SYNC_S = 5.0



@dataclass
class Status:
    """One snapshot of the gaussmeter, for status() and the wire."""

    connected: bool
    idn: str = ""
    probe: str = ""                    # probe family: HST / HSE / UHS ("" = none detected)
    probe_serial: str = ""
    probe_type_code: int = -1          # the 455's TYPE? answer
    probe_sensitivity_mV_per_kG: float = _NAN
    probe_geometry: str = "axial"      # from config: the 455 does not report it
    probe_desc: str = ""               # one line for panels and logs
    hw_error: str = ""
    # the live reading
    field_mT: float = _NAN             # what the meter reads now (DC, RMS or peak field)
    measured_field_mT: float = _NAN    # DC field only (NaN in rms/peak) -- for field-source subscribers
    quantity: str = "DC field"         # what field_mT and the acquired sample ARE
    field_rel_mT: float = _NAN         # field - rel_setpoint, when relative mode is on
    flag: str = ""                     # "", "overload", "no probe"
    read_ms: float = _NAN              # how long the last reading took
    readings: int = 0                  # live readings since start
    # settings: what we asked for (_set) and what the meter applied
    mode: str = "dc"
    dc_digits: int = 4
    rms_band: str = "wide"
    peak_mode: str = "periodic"        # adopted from the meter, never changed here
    peak_display: str = "positive"
    auto_range: bool = True
    range_set_mT: float = _NAN
    range_mT: float = _NAN
    range_min_mT: float = _NAN
    range_max_mT: float = _NAN
    ranges_mT: list = field(default_factory=list)
    display_unit: str = "G"
    relative: bool = False
    rel_setpoint_mT: float = 0.0
    settle_s: float = 0.0
    # probe zero
    zeroing: bool = False
    # acquisition
    acq_readings: int = 1
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf -- NaN passes every `<`/`>` clamp and
    would be sent straight to the instrument."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


class Gaussmeter:
    def __init__(self, backend: GaussmeterBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards readings + acquisition

        self._connected = False
        self._idn = ""
        self._probe: dict = {}
        # the meter's peak sub-settings, READ from it (this module never sets them)
        self._peak = ("periodic", "positive")
        self._hw_error = ""
        self._last_err_emit = -1e9

        # what the meter reports (written under _hw by setters / start)
        self._ranges: list[float] = []
        self._range_actual = _NAN

        # written only by the polling thread (under _lock)
        self._field = _NAN
        self._flag = FLAG_OK
        self._read_ms = _NAN
        self._n_read = 0
        self._zeroing = False
        self._reread_pending = False        # set by the poll thread when a probe reappears
        # The front-panel state as last READ from (or written to) the meter.
        # The periodic re-read compares against THIS, not against cfg: a
        # setter writes cfg first and the meter a moment later, and comparing
        # with cfg in between would "adopt" the old value over the user's.
        self._panel: dict = {}
        self._next_sync = 0.0
        self._sync_now = False              # set by acquire(): re-read first

        # acquisition state (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the meter, READ and adopt its state, start polling.

        Nothing is written to the meter here (see the module docstring): the
        455 keeps its settings over a power cycle and someone may have set it
        up on the front panel, so connecting must not change a measurement.
        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        m = self.cfg.meter
        with self._hw:
            # open() claims the meter's address (hwlock) -- a HardwareBusy
            # from it means another service owns the meter; we never talked
            # to it, so there is nothing to close and it simply propagates.
            self.backend.open()
            try:
                self._idn = self._try(self.backend.idn, "")
                self._connected = True
                self._read_probe()
                # every one of these is a QUERY
                mode, digits, band = self.backend.get_mode()
                m.mode = mode if mode in MODES else "dc"
                m.dc_digits, m.rms_band = int(digits), band
                self._peak = tuple(self.backend.get_peak())
                m.auto_range = bool(self.backend.get_auto_range())
                if not m.auto_range:
                    m.range_mT = float(self.backend.get_range())
                m.display_unit = self.backend.get_display_unit()
                m.relative, m.rel_setpoint_mT = self.backend.get_relative()
                m.relative = bool(m.relative)
                m.rel_setpoint_mT = float(m.rel_setpoint_mT)
                self._read_back()
            except BaseException:
                # adoption failed half way: close the port so the address is
                # released (a failed start must not keep the meter "busy")
                self._connected = False
                try:
                    self.backend.close()
                except Exception:
                    pass
                raise
        self._next_sync = self._clock() + FRONT_PANEL_SYNC_S
        self._emit("info", f"connected: {self._idn or 'gaussmeter'}")
        self._emit_probe(first=True)
        self._emit("info", "adopted the meter's settings: "
                           f"{self.quantity()}, "
                           + (f"{m.dc_digits} digits, " if m.mode == "dc" else
                              f"{m.rms_band} band, " if m.mode == "rms" else
                              f"{self._peak[0]} peak, ")
                           + ("auto range" if m.auto_range else
                              f"manual range {_fmt_mT(self._range_actual)}")
                           + f", front panel in {m.display_unit}"
                           + (f", relative to {_fmt_mT(m.rel_setpoint_mT)}" if m.relative else ""))
        if m.mode == "peak":
            # not an error, but the one adopted state that changes what a scan
            # records: say it where people look
            self._emit("warn", "the meter is in PEAK mode: field readings and "
                               "acquisitions are the " + self.quantity()
                               + ", and measured_field_mT (the field-source key) is empty")
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="ls455-poll", daemon=True)
            self._thread.start()

    # ---- the probe ------------------------------------------------------------

    def _read_probe(self) -> bool:
        """Ask the meter which probe is plugged in (with _hw held). Returns True
        if the family -- and therefore the range list -- changed."""
        old_family, old_ranges = self._probe.get("family"), list(self._ranges)
        self._probe = self._try(self.backend.probe_info, {})
        self._ranges = list(self.backend.ranges_mT())
        return bool(old_ranges) and (self._probe.get("family") != old_family
                                     or self._ranges != old_ranges)

    def reread_probe(self) -> None:
        """Re-read the probe (after swapping it). The ranges, the range limits
        and therefore describe follow the new probe; describe_rev moves when
        the range list changes, so scan-core re-fetches the limits."""
        if not self._connected:
            raise ValueError("not connected")
        with self._hw:
            changed = self._read_probe()
            if not self.cfg.meter.auto_range:
                self.cfg.meter.range_mT = float(self.backend.get_range())
            self._read_back()
        self._emit_probe(changed=changed)

    def probe_desc(self) -> str:
        """'HSE axial, S/N H12345, 8 mV/kG' -- one line for the panel and the log."""
        p = self._probe
        fam = p.get("family") or ""
        if not fam:
            return "no probe identified"
        geo = self.cfg.hardware.probe_geometry
        parts = [f"{fam} {geo}"]
        if p.get("serial"):
            parts.append(f"S/N {p['serial']}")
        sens = p.get("sensitivity_mV_per_kG", _NAN)
        if isinstance(sens, (int, float)) and math.isfinite(sens):
            parts.append(f"{sens:g} mV/kG")
        return ", ".join(parts)

    def _emit_probe(self, first: bool = False, changed: bool = False) -> None:
        if not self._probe.get("family"):
            self._emit("warn", "no probe identified (TYPE? gave no known type): "
                               f"ranges assume {', '.join(_fmt_mT(r) for r in self._ranges)}")
            return
        rng = f"ranges {_fmt_mT(min(self._ranges))} .. {_fmt_mT(max(self._ranges))}"
        if changed:
            self._emit("warn", f"PROBE CHANGED: now {self.probe_desc()}; {rng}")
        else:
            self._emit("info", f"probe: {self.probe_desc()} (geometry from config); {rng}"
                       if first else f"probe re-read: {self.probe_desc()}; {rng}")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop polling and disconnect. Safe to call more than once. A
        gaussmeter drives nothing, so there is no output to make safe; its
        settings are left as they are for the next user of the front panel.

        keep_outputs (the restart flag of the universal `shutdown` verb,
        2026-10-06) changes nothing here: this shutdown never changes an
        output, so every stop already leaves the instrument as it is.
        """
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
            if was:
                self._emit("info", "disconnected")

    # ---- limits -----------------------------------------------------------------

    def range_limits(self) -> tuple[float, float]:
        """The config envelope narrowed by the ranges the probe actually has."""
        lim = self.cfg.limits
        lo, hi = lim.range_min_mT, lim.range_max_mT
        if self._ranges:
            lo = max(lo, min(self._ranges))
            hi = min(hi, max(self._ranges))
        return lo, hi

    def settle_s(self) -> float:
        """How long after a trigger the meter's filter needs before a reading
        represents the new field (see Acquisition.settle_time_constants)."""
        m = self.cfg.meter
        if m.mode == "rms":
            tau = RMS_TIME_CONSTANT_S
        elif m.mode == "peak":
            tau = PEAK_TIME_CONSTANT_S
        else:
            tau = DC_TIME_CONSTANT_S.get(int(m.dc_digits), 0.1)
        return float(self.cfg.acquisition.settle_time_constants) * tau

    def acquire_timeout_s(self) -> float:
        """How long a client should wait for one acquisition before giving up.

        The configured `timeout_s` is a MARGIN on top of what the acquisition
        needs by construction: the settling wait plus the readings. A reading
        is budgeted at 0.5 s (the real meter manages ~6 per second on auto
        range: three queries 50 ms apart), so 1000 readings at 5 digits are
        not declared failed after 30 s while they are still running fine."""
        n = max(1, int(self.cfg.acquisition.readings))
        return float(self.cfg.acquisition.timeout_s) + self.settle_s() + 0.5 * n

    # ---- settings (each validates/clamps, stores in cfg, pushes, reads back) ----

    def set_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.cfg.meter.mode = mode
        self._push_mode()
        self._emit("info", f"{mode.upper()} mode: readings are the {self.quantity()}"
                   + (" (measured_field_mT is empty outside DC)" if mode != "dc" else ""))

    def set_dc_digits(self, digits: int) -> None:
        n = int(round(_finite(digits, "dc_digits")))
        value, clamped = _clamp(n, min(DC_DIGITS), max(DC_DIGITS))
        self.cfg.meter.dc_digits = int(value)
        self._push_mode()
        self._emit("warn" if clamped else "info",
                   f"DC resolution {int(value)} digits ({DC_BANDWIDTH_HZ[int(value)]:g} Hz bandwidth)"
                   + (" (clamped)" if clamped else ""))

    def set_rms_band(self, band: str) -> None:
        band = str(band).lower()
        if band not in RMS_BANDS:
            raise ValueError(f"rms_band must be one of {RMS_BANDS}, got {band!r}")
        self.cfg.meter.rms_band = band
        self._push_mode()
        self._emit("info", f"RMS band {band}")

    def set_auto_range(self, on: bool) -> None:
        m = self.cfg.meter
        m.auto_range = bool(on)
        if self._connected:
            with self._hw:
                self.backend.set_auto_range(m.auto_range)
                if not m.auto_range:
                    # Hand over from auto to manual WITHOUT a jump: keep the
                    # range auto had chosen, and remember it as the setpoint.
                    m.range_mT = float(self.backend.get_range())
                self._read_back()
        self._emit("info", "auto range on" if m.auto_range else
                   f"manual range {_fmt_mT(self._range_actual)}")

    def set_range(self, full_scale_mT: float) -> None:
        """Manual range. Switches auto-range off: asking for a range and then
        letting auto overrule it would silently ignore the request."""
        lo, hi = self.range_limits()
        asked = _finite(full_scale_mT, "range")
        value, clamped = _clamp(asked, lo, hi)
        m = self.cfg.meter
        m.range_mT = value
        m.auto_range = False
        if self._connected:
            with self._hw:
                self.backend.set_auto_range(False)
                self.backend.set_range(value)
                self._read_back()
        msg = f"range {_fmt_mT(self._range_actual)} (asked {_fmt_mT(asked)})"
        self._emit("warn" if clamped else "info",
                   msg + (f", clamped to {_fmt_mT(lo)}..{_fmt_mT(hi)}" if clamped else ""))

    def set_display_unit(self, unit: str) -> None:
        """Front-panel unit only; readings on the wire stay in mT."""
        if unit not in UNIT_CODES:
            raise ValueError(f"unit must be one of {list(UNIT_CODES)}, got {unit!r}")
        self.cfg.meter.display_unit = unit
        if self._connected:
            with self._hw:
                self.backend.set_display_unit(unit)
                # RELSP is stored in display units on the meter: re-send it
                m = self.cfg.meter
                self.backend.set_relative(m.relative, m.rel_setpoint_mT)
                self._record_panel()
        self._emit("info", f"front panel shows {unit}")

    def set_relative(self, on: bool, setpoint_mT: float | None = None) -> None:
        m = self.cfg.meter
        if setpoint_mT is not None:
            lim = self.cfg.limits.rel_setpoint_max_mT
            value, clamped = _clamp(_finite(setpoint_mT, "relative setpoint"), -lim, lim)
            m.rel_setpoint_mT = value
            if clamped:
                self._emit("warn", f"relative setpoint clamped to {value:g} mT")
        m.relative = bool(on)
        if self._connected:
            with self._hw:
                self.backend.set_relative(m.relative, m.rel_setpoint_mT)
        self._emit("info", f"relative {'on' if m.relative else 'off'}, "
                           f"setpoint {m.rel_setpoint_mT:g} mT")

    def relative_here(self) -> None:
        """Relative mode on, with the present field as the setpoint."""
        with self._lock:
            b = self._field
            bad = self._hw_error or self._flag or self._zeroing
        if not math.isfinite(b) or bad:
            # a stale, clipped or missing reading must not become the setpoint
            raise ValueError("no valid reading right now")
        self.set_relative(True, b)

    def set_acquisition(self, readings: int) -> None:
        lim = self.cfg.limits
        n = int(round(_finite(readings, "readings")))
        value, clamped = _clamp(n, lim.readings_min, lim.readings_max)
        self.cfg.acquisition.readings = int(value)
        self._emit("warn" if clamped else "info",
                   f"acquire averages {int(value)} readings"
                   + (" (clamped)" if clamped else ""))

    # ---- probe zero ----------------------------------------------------------

    def zero(self) -> None:
        """Zero the probe. THE PROBE MUST BE IN THE ZERO-GAUSS CHAMBER --
        whatever field it sees now (a magnet, Earth's 0.05 mT) becomes zero,
        and every later reading is off by that much."""
        if not self._connected:
            raise ValueError("not connected")
        with self._lock:
            if self._acq is not None:
                raise ValueError("an acquisition is running; zero afterwards")
        with self._hw:
            self.backend.start_zero()
        with self._lock:
            self._zeroing = True
        self._emit("warn", "zero probe started (probe must be in the zero-gauss chamber)")

    def clear_zero(self) -> None:
        if self._connected:
            with self._hw:
                self.backend.clear_zero()
        self._emit("info", "probe zero cleared")

    # ---- the scan-safe read ---------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        if not self._connected:
            raise ValueError("not connected")
        n = max(1, int(self.cfg.acquisition.readings))
        settle = self.settle_s()
        with self._lock:
            if self._zeroing:
                raise ValueError("zero probe running; no readings possible")
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": self._clock() + settle, "want": n,
                         "vals": [], "taken": 0, "flags": set()}
            # re-read the front panel before the first reading that counts, so
            # the unit conversion and the reported quantity are the meter's NOW
            self._sync_now = True
            return self._acq_id

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        m = self.cfg.meter
        rlo, rhi = self.range_limits()
        settle = self.settle_s()
        with self._lock:
            a = self._acq
            b = self._field
            # A number that is not a measurement must not look like one: after
            # a failed read, during a zero, with no probe or in overload the
            # last value is stale or clipped. measured_field_mT is what a field
            # SOURCE subscriber (e.g. a VNA) files with its data, so it goes
            # NaN; field_mT keeps the raw value for the panel, next to `flag`.
            valid = (self._connected and not self._hw_error and not self._zeroing
                     and not self._flag and math.isfinite(b))
            return Status(
                connected=self._connected, idn=self._idn,
                probe=self._probe.get("family", ""),
                probe_serial=str(self._probe.get("serial", "")),
                probe_type_code=int(self._probe.get("type_code", -1)),
                probe_sensitivity_mV_per_kG=float(
                    self._probe.get("sensitivity_mV_per_kG", _NAN)),
                probe_geometry=self.cfg.hardware.probe_geometry,
                probe_desc=self.probe_desc(),
                hw_error=self._hw_error,
                field_mT=b,
                measured_field_mT=b if (m.mode == "dc" and valid) else _NAN,
                quantity=self.quantity(),
                field_rel_mT=(b - m.rel_setpoint_mT) if m.relative else _NAN,
                flag=self._flag, read_ms=self._read_ms, readings=self._n_read,
                mode=m.mode, dc_digits=int(m.dc_digits), rms_band=m.rms_band,
                peak_mode=self._peak[0], peak_display=self._peak[1],
                auto_range=m.auto_range, range_set_mT=m.range_mT,
                range_mT=self._range_actual, range_min_mT=rlo, range_max_mT=rhi,
                ranges_mT=list(self._ranges), display_unit=m.display_unit,
                relative=m.relative, rel_setpoint_mT=m.rel_setpoint_mT,
                settle_s=settle,
                zeroing=self._zeroing,
                acq_readings=int(self.cfg.acquisition.readings),
                acq_id=self._acq_id, acquiring=a is not None,
                acq_progress=0.0 if a is None else a["taken"] / a["want"],
                sample=dict(self._sample),
            )

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire) and push it."""
        self._sanitise_config()
        if self._connected:
            with self._hw:
                self._push_meter()
        self._emit("info", "settings applied")

    # ---- polling --------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
            t = self._clock()
            self.poll_once()
            # A reading itself takes tens of ms on GPIB/serial; only sleep what
            # is left of the period.
            self._stop.wait(max(0.005, period - (self._clock() - t)))

    def poll_once(self) -> None:
        """One reading (or one zero-state check), then advance any acquisition.
        Public so tests and single-threaded scripts can drive it."""
        with self._lock:
            zeroing = self._zeroing
        try:
            if zeroing:
                with self._hw:
                    running = self.backend.zero_running()
                if not running:
                    with self._lock:
                        self._zeroing = False
                    self._emit("info", "zero probe finished")
                return
            if self._sync_now or self._clock() >= self._next_sync:
                self._sync_now = False
                self._next_sync = self._clock() + FRONT_PANEL_SYNC_S
                self._sync_front_panel()
            with self._hw:
                t_start = self._clock()
                value, flag = self.backend.read_field()
                t_end = self._clock()
                rng = self.backend.get_range() if self.cfg.meter.auto_range else None
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        with self._lock:
            recovered = bool(self._hw_error)
            # A probe that was missing (or a link that was down) and is back
            # may be a DIFFERENT probe: re-read it so the range list is right.
            reread = recovered or (self._flag == FLAG_NO_PROBE and flag != FLAG_NO_PROBE)
            self._hw_error = ""
            self._field = float(value)
            self._flag = flag
            self._read_ms = (t_end - t_start) * 1e3
            self._n_read += 1
            if rng is not None:
                self._range_actual = float(rng)
            self._advance_acquisition(t_start, float(value), flag)
        if recovered:
            self._emit("info", "hardware reads recovered")
        if reread:
            try:
                self.reread_probe()
            except Exception as exc:          # never let the polling thread die
                self._report_hw_error(exc)

    def _advance_acquisition(self, t_start: float, value: float, flag: str) -> None:
        """Called with _lock held. Only readings that STARTED after the trigger
        plus the filter's settling time count; the result and "done" are
        published in this same critical section (gotcha #28)."""
        a = self._acq
        if a is None or t_start < a["t0"]:
            return
        # EVERY reading counts toward `want`, but only valid ones enter the
        # mean: an overloaded reading is clipped at full scale and a "no probe"
        # one is meaningless. Counting them anyway means an acquisition always
        # FINISHES (with NaN and a flag) instead of hanging until the client's
        # timeout while the range is too small.
        a["taken"] = a["taken"] + 1
        if flag:
            a["flags"].add(flag)
        elif math.isfinite(value):
            a["vals"].append(value)
        if a["taken"] < a["want"]:
            return
        vals = a["vals"]
        n = len(vals)
        mean = sum(vals) / n if n else _NAN
        std = (math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1
               else (0.0 if n == 1 else _NAN))
        self._sample = {"acq_id": a["id"], "field_mT": mean, "std_mT": std, "n": n,
                        "flag": ",".join(sorted(a["flags"])),
                        "mode": self.cfg.meter.mode, "quantity": self.quantity(),
                        "range_mT": self._range_actual,
                        "time": time.time()}
        self._acq = None

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"hardware read failed: {msg}")

    # ---- internals ---------------------------------------------------------------------

    def _sanitise_config(self) -> None:
        m, lim = self.cfg.meter, self.cfg.limits
        if str(m.mode).lower() not in MODES:
            m.mode = "dc"
        m.mode = str(m.mode).lower()
        if str(m.rms_band).lower() not in RMS_BANDS:
            m.rms_band = "wide"
        m.rms_band = str(m.rms_band).lower()
        m.dc_digits = int(_clamp(int(m.dc_digits), min(DC_DIGITS), max(DC_DIGITS))[0])
        if m.display_unit not in UNIT_CODES:
            m.display_unit = "G"
        hw = self.cfg.hardware
        if str(hw.probe_geometry).lower() not in PROBE_GEOMETRIES:
            hw.probe_geometry = "axial"
        hw.probe_geometry = str(hw.probe_geometry).lower()
        m.range_mT = _clamp(float(m.range_mT), *self.range_limits())[0]
        r = lim.rel_setpoint_max_mT
        m.rel_setpoint_mT = _clamp(float(m.rel_setpoint_mT), -r, r)[0]
        a = self.cfg.acquisition
        a.readings = int(_clamp(int(a.readings), lim.readings_min, lim.readings_max)[0])

    def _push_mode(self) -> None:
        if self._connected:
            m = self.cfg.meter
            with self._hw:
                self.backend.set_mode(m.mode, int(m.dc_digits), m.rms_band)
                self._read_back()

    def _push_meter(self) -> None:
        """Send every meter setting (with _hw held)."""
        m = self.cfg.meter
        self.backend.set_mode(m.mode, int(m.dc_digits), m.rms_band)
        self.backend.set_display_unit(m.display_unit)
        self.backend.set_auto_range(m.auto_range)
        if not m.auto_range:
            self.backend.set_range(m.range_mT)
        self.backend.set_relative(m.relative, m.rel_setpoint_mT)
        self._read_back()

    def _read_back(self) -> None:
        """What the meter actually applied (with _hw held). Queries only."""
        self._range_actual = float(self.backend.get_range())
        self._peak = tuple(self.backend.get_peak())
        self._record_panel()

    def _record_panel(self) -> None:
        """Remember the meter's state as it is now known (after a read or a
        push), for the periodic front-panel re-read to compare against."""
        m = self.cfg.meter
        self._panel = {"unit": m.display_unit, "mode": m.mode,
                       "dc_digits": int(m.dc_digits), "rms_band": m.rms_band,
                       "peak": tuple(self._peak), "auto_range": bool(m.auto_range),
                       "range": None if m.auto_range else float(self._range_actual)}

    def _sync_front_panel(self) -> None:
        """Re-read the settings someone may have changed by hand on the meter,
        and adopt the ones that differ from what was last known. QUERIES ONLY.
        Called from the polling thread; raises on a failed read (the caller
        reports it as a hardware error)."""
        m = self.cfg.meter
        with self._hw:
            unit = self.backend.get_display_unit()
            mode, digits, band = self.backend.get_mode()
            peak = tuple(self.backend.get_peak())
            auto = bool(self.backend.get_auto_range())
            rng = None if auto else float(self.backend.get_range())
            now = {"unit": unit, "mode": mode if mode in MODES else "dc",
                   "dc_digits": int(digits), "rms_band": band, "peak": peak,
                   "auto_range": auto, "range": rng}
            changed = [k for k, v in now.items() if v != self._panel.get(k)]
            if not changed:
                return
            m.display_unit = unit
            m.mode, m.dc_digits, m.rms_band = now["mode"], int(digits), band
            self._peak = peak
            m.auto_range = auto
            if rng is not None:
                m.range_mT = rng
                self._range_actual = rng
            self._panel = now
        with self._lock:
            if self._acq is not None and self._acq["taken"] > 0:
                # this acquisition already counted readings taken BEFORE the
                # change: its mean mixes two settings. (A change adopted before
                # its first counted reading -- the re-read acquire() asks for --
                # is harmless and not flagged.)
                self._acq["flags"].add("settings changed")
        what = ", ".join(f"{k}={'/'.join(v) if isinstance(v, tuple) else v}"
                         for k, v in now.items() if k in changed)
        self._emit("warn", f"front panel changed on the meter, adopted: {what}; "
                           f"readings are now the {self.quantity()}")

    def quantity(self) -> str:
        """What field_mT and an acquired sample ARE, in words -- a detector must
        say what it reports, most of all in a mode adopted from the front panel."""
        mode = self.cfg.meter.mode
        if mode == "rms":
            return f"RMS field ({self.cfg.meter.rms_band} band)"
        if mode == "peak":
            pm, pd = self._peak
            which = {"positive": "positive peak", "negative": "negative peak",
                     "both": "larger peak"}.get(pd, "peak")
            return f"{which} field ({pm})"
        return "DC field"

    @staticmethod
    def _try(fn, default):
        """For optional queries: a meter that refuses one should not stop start-up."""
        try:
            return fn()
        except Exception:
            return default

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _fmt_mT(value: float) -> str:
    """Human-scaled field for the event log (ASCII: 'uT')."""
    v = float(value)
    if not math.isfinite(v):
        return "--"
    if abs(v) >= 1000:
        return f"{v / 1000:.4g} T"
    if abs(v) >= 1:
        return f"{v:.4g} mT"
    return f"{v * 1000:.4g} uT"
