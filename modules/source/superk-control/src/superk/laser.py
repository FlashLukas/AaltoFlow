"""SuperK: the small "brain" between the wire and the backend.

A supercontinuum laser with an AOTF filter is a SET-AND-FORGET instrument: you
command emission, a power level, which crystal, and up to 8 lines (wavelength +
RF amplitude each), and the hardware holds them. So there is no control loop
here. What there IS, because this is a CLASS 4 LASER, is safety logic:

  * starting the service only READS the laser and adopts what it is doing
    (Lukas, 2026-09-27: "read the instrument state on startup, not change
    anything") -- emission is never switched on by a start, and an emission
    someone left on is shown as ON, not silently switched off;
  * a remote GUI that switches emission on "owns" it and pings; if it goes
    silent for hardware.client_timeout_s the service switches emission OFF
    (lost-client guard). A scan routine switches it on without an owner, so a
    scan that does not talk to the laser for an hour is never cut;
  * `set_emission(True)` is REFUSED (SafetyError) while the interlock is not OK
    or the laser is not connected -- the caller gets {"ok": false, "error": ...};
  * the power level is clamped to `limits.power_max_pct`, amplitudes to
    `limits.amplitude_max_pct`, wavelengths to the ACTIVE crystal's range,
    and every clamp is announced as a warn event;
  * `shutdown()` switches RF and emission OFF before it disconnects, and the
    laser's own watchdog (hardware.watchdog_s) switches emission off if the
    process dies without running any code at all;
  * if the interlock opens while emitting, the brain notices it in the poll,
    forgets the "emission on" request (so closing the door does not bring the
    beam back by itself) and says so in a warn event.

WAVELENGTH SWEEP (fly scans, 2026-10-10; INSTRUMENT_MODULE_GUIDE.md 6b
"Ramps"). Any ONE line's wavelength can be swept at a set pace
(ramp_wavelength): the SERVICE walks the wavelength register (softramp.py, a
SOFTWARE ramp -- the SELECT has no sweep of its own a fly scan could follow)
and records every value it sent. A fly scan bins by that COMMANDED wavelength
(the AOTF follows its RF frequency within microseconds, so the command is the
wavelength far better than a pixel). One sweep at a time, one ramp_id
counter: a new sweep (of any line) replaces a running one. A sweep never
touches emission, RF, power or amplitudes -- only the wavelength of its line.

THREADS (docs/DEVELOPER_NOTES.md gotcha #1). One worker thread polls the
hardware and REBUILDS the Status snapshot; setters change brain attributes
(the desired values) and write to the hardware, and never touch the snapshot.
`status()` just returns the last snapshot -- it never talks to hardware, so a
slow serial port cannot stall the publisher or the GUI. All backend calls go
through ONE lock, because the Interbus port can only do one thing at a time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace

from . import config as C
from .backends.base import SupercontinuumBackend
from .config import Config, N_LINES
from .softramp import SoftRamp
from .stream import StreamRecorder

INTERLOCK_TEXT = {0: "open", 1: "needs reset", 2: "OK"}

#: Every value Status.emission_state can take. describe declares it an enum
#: with exactly these options: scan-core stores an enum as a code, and a value
#: missing from the list would be recorded as "not measured" (developer notes 4b).
EMISSION_STATES = ("off", "starting", "on", "interlock", "error")


class SafetyError(ValueError):
    """A request refused for safety reasons (e.g. emission with an open interlock).
    A ValueError, so the service turns it into {"ok": false, "error": ...}."""


@dataclass
class Status:
    """One snapshot of the laser, for status() and the wire.

    `*_set` fields are what was COMMANDED, the plain ones what the hardware
    REPORTS. A scan waits until the reported value echoes the commanded one.
    """

    connected: bool = False
    idn: str = ""
    # EXTREME
    emission_set: bool = False
    emission_on: bool = False
    emission_state: str = "off"        # one of EMISSION_STATES (describe's enum)
    interlock_ok: bool = False
    interlock_code: int = 0
    interlock: str = "open"
    status_bits: int = 0
    power_set_pct: float = 0.0
    power_pct: float = 0.0
    inlet_temp_C: float = 0.0
    # SELECT RF driver
    rf_set: bool = False
    rf_on: bool = False
    # name of the active crystal; None = no crystal table / not connected yet.
    # None, not "": describe declares an enum of the table's names, "" is not
    # one of them, and scan-core would silently lose the point.
    filter: str | None = None
    filter_min_nm: float = 0.0
    filter_max_nm: float = 0.0
    crystal_temp_C: float = 0.0
    wavelength_set_nm: list = field(default_factory=lambda: [0.0] * N_LINES)
    wavelength_nm: list = field(default_factory=lambda: [0.0] * N_LINES)
    amplitude_set_pct: list = field(default_factory=lambda: [0.0] * N_LINES)
    amplitude_pct: list = field(default_factory=lambda: [0.0] * N_LINES)
    crystal: int = 0                   # NKT crystal number the RF driver reports (0 = none)
    emission_guarded: bool = False     # lost-client guard armed (a remote GUI owns emission)
    hw_error: str = ""
    # The wavelength SWEEP (ramp_wavelength, fly scans). Live values of the
    # software ramp, laid over the snapshot by status() (in memory: no
    # hardware is read for them). ramp_id = the newest sweep started; "ramp_id
    # >= mine and not ramping" = my sweep is over. ramp_line is 1-based (0 =
    # none yet). 0.0, not NaN, for "none": this status goes out as plain JSON.
    ramping: bool = False
    ramp_id: int = 0
    ramp_line: int = 0
    ramp_target_nm: float = 0.0
    ramp_rate_nm_per_s: float = 0.0


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class SuperK:
    def __init__(self, backend: SupercontinuumBackend, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        # replaced by the service to forward events; default = no-op. Set
        # first, because the checks below may already announce something.
        self._on_event = lambda level, msg: None
        self._lock = threading.RLock()          # every backend call goes under it
        self._connected = False
        self._idn = ""
        s = self.cfg.startup
        # ---- desired state (the brain's attributes; gotcha #1) ----------
        self._emission = False                  # NEVER True at construction
        self._rf = False                        # RF starts off too
        self._power = float(s.power_pct)
        self._filter = self._filter_index(s.filter)
        self._range = self._config_range(self._filter)
        # NKT crystal number of the ACTIVE table entry when it was last
        # switched to / adopted: apply_config re-switches only if someone edits
        # the table so that this number changes
        self._applied_code = None
        self._sanitise_limits()
        self._wl = C.floats(s.wavelengths_nm, N_LINES, 0.0)
        self._amp = C.floats(s.amplitudes_pct, N_LINES, 0.0)
        # presets as last seen: apply_config sends only the ones that CHANGED
        self._presets_seen = dict(vars(s))
        # ---- lost-client guard (see set_emission / touch) ------------------
        self._owner: str | None = None          # client id that owns emission
        self._owner_seen = 0.0                  # monotonic time of its last word
        # ---- the wavelength SWEEP (see the module doc) ---------------------
        # ONE SoftRamp for all 8 lines: one sweep at a time and one ramp_id
        # counter. _ramp_line (0-based) says which line its steps write; it is
        # changed only between stop() and start(), never under a running walk.
        self._ramp_line = 0
        self._ramp = SoftRamp(self._ramp_step, lambda: self._wl[self._ramp_line],
                              limits=lambda: self._range,
                              dt_s=float(self.cfg.hardware.ramp_dt_s),
                              on_done=self._ramp_done, channel="wavelength",
                              name="superk-sweep")
        # THE STREAM: every wavelength the sweep sent, as ONE row of all 8
        # lines (the lines not being swept keep their last value: forward-
        # filled), so a fly scan over any line finds its channel in one group.
        self.recorder = StreamRecorder([f"wavelength_{n}" for n in range(1, N_LINES + 1)])
        # ---- snapshot, rebuilt only by the worker -------------------------
        self._status = Status()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ================================================================ filters

    def filter_names(self) -> list[str]:
        return C.names(self.cfg.filters.names)

    def _filter_index(self, name) -> int:
        """Index of a filter by name (case-insensitive) or by number; 0 if unknown."""
        names = self.filter_names()
        if isinstance(name, int) or (isinstance(name, str) and name.strip().isdigit()):
            i = int(name)
            return i if 0 <= i < len(names) else 0
        low = [n.lower() for n in names]
        return low.index(str(name).strip().lower()) if str(name).strip().lower() in low else 0

    def _config_range(self, idx: int) -> tuple[float, float]:
        n = len(self.filter_names())
        lo = C.floats(self.cfg.filters.min_nm, n, 400.0)
        hi = C.floats(self.cfg.filters.max_nm, n, 2400.0)
        idx = min(max(idx, 0), max(n - 1, 0))
        return (lo[idx], hi[idx]) if n else (400.0, 2400.0)

    def _crystal_code(self, idx: int) -> int:
        n = len(self.filter_names())
        return C.ints(self.cfg.filters.crystal, n, 1)[idx] if n else 1

    def wavelength_range(self) -> tuple[float, float]:
        """The LIVE allowed wavelength range: the active crystal's. describe
        reads this, so its revision moves when the crystal changes."""
        return self._range

    def active_filter(self) -> str:
        names = self.filter_names()
        return names[self._filter] if names else ""

    # ============================================================== lifecycle

    def start(self) -> None:
        """Connect and ADOPT the laser's state. Emission is NOT switched on --
        and, since 2026-09-27, nothing else is changed either: RF, power level,
        crystal and the 8 lines are READ and become the brain's setpoints, so
        the GUI and describe show what the laser is really doing.

        The one write that can remain is the laser's watchdog (a safety
        interlock against a killed service), and only if the laser's own value
        differs from hardware.watchdog_s. hardware.emission_off_on_start
        (default False) is an opt-in to switch emission off here."""
        hw = self.cfg.hardware
        with self._lock:
            self.backend.open()
            self._connected = True
            self._idn = self.backend.identify()
            if hw.emission_off_on_start:
                self.backend.set_emission(False)
            self._arm_watchdog_locked()
            self._adopt_locked()
        self._emit("info", f"connected: {self._idn or 'SuperK'} -- adopted: emission "
                           f"{'ON' if self._emission else 'off'}, RF "
                           f"{'ON' if self._rf else 'off'}, power {self._power:g} %, "
                           f"crystal {self.active_filter()}")
        if self._emission:
            self._emit("warn", "the laser was already EMITTING at start; left on "
                               "(no lost-client guard until a GUI switches it on)")
        self._poll_once()
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="superk-poll",
                                        daemon=True)
        self._thread.start()

    def _adopt_locked(self) -> None:
        """Read every setpoint the laser holds and make it the brain's own.
        QUERIES ONLY. A value outside this module's limits is NOT corrected on
        the laser (that would be a write at start): it is announced, and the
        next explicit request is clamped as usual. Caller holds self._lock."""
        b = self.backend
        self._emission = bool(b.read_emission())
        self._owner = None                      # nobody here switched it on
        self._rf = bool(b.read_rf())
        self._power = float(b.read_power())
        lim = self.cfg.limits
        if not lim.power_min_pct <= self._power <= lim.power_max_pct:
            self._emit("warn", f"the laser's power level {self._power:g} % is outside "
                               f"this module's limits {lim.power_min_pct:g}.."
                               f"{lim.power_max_pct:g} %; left as it is")
        # crystal: which one the RF driver reaches, mapped onto the table
        try:
            code = int(b.read_crystal())
        except Exception:
            code = 0
        n = len(self.filter_names())
        codes = C.ints(self.cfg.filters.crystal, n, 1) if n else []
        if code in codes:
            self._filter = codes.index(code)
        else:
            self._emit("warn", f"the RF driver reports crystal {code}, which is not in "
                               f"the filter table ({self.cfg.filters.crystal}); showing "
                               f"{self.active_filter()} -- check filters.crystal")
        self._applied_code = self._crystal_code(self._filter)
        rng = b.read_crystal_range() or self._config_range(self._filter)
        self._range = rng
        self._wl = [float(b.read_wavelength(i)) for i in range(N_LINES)]
        self._amp = [float(b.read_amplitude(i)) for i in range(N_LINES)]
        lo, hi = rng
        for i in range(N_LINES):
            if self._amp[i] > 0 and not lo <= self._wl[i] <= hi:
                self._emit("warn", f"line {i + 1}: {self._wl[i]:g} nm is outside "
                                   f"{self.active_filter()} ({lo:g}..{hi:g} nm); "
                                   f"left as it is")

    def _arm_watchdog_locked(self) -> None:
        """Make the laser's own watchdog match hardware.watchdog_s. It is the
        only protection left when the service is KILLED (no code runs then),
        so it is a safety interlock and the one write a start may do -- and
        only when the laser's value differs. Caller holds self._lock."""
        want = max(0, int(self.cfg.hardware.watchdog_s))
        try:
            have = int(self.backend.read_watchdog())
        except Exception:
            have = None                         # unreadable: write to be sure
        if have != want:
            self.backend.set_watchdog(want)
            self._emit("info", f"laser watchdog {have if have is not None else '?'} s "
                               f"-> {want} s (safety against a killed service)")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """RF off, emission off, disconnect. Safe to call more than once / on a crash.

        A running wavelength sweep is stopped first (no step may follow the RF
        off below).

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the port the same, but leave emission and RF
        as they are -- the next start adopts them. The laser's own watchdog
        (hardware.watchdog_s) is NOT touched: if the new service is not
        talking to the laser within that time, the laser cuts emission
        itself, as after a killed service."""
        self._stop.set()
        # stopped BEFORE self._lock, which a sweep step may be waiting for
        self._ramp.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            try:
                if self._connected and not keep_outputs:
                    for fn in (lambda: self.backend.set_rf(False),
                               lambda: self.backend.set_emission(False)):
                        try:
                            fn()
                        except Exception as exc:          # keep going: the other
                            self._emit("error", f"shutdown: {exc}")  # must still run
                    self._rf = False
                    self._emission = False
            finally:
                try:
                    self.backend.close(outputs_off=not keep_outputs)
                finally:
                    was = self._connected
                    self._connected = False
                    st = Status(**{**self._status.__dict__})
                    st.connected = False
                    self._status = st
                    if was:
                        self._emit("info", "disconnected, emission and RF left as "
                                           "they are (restart)" if keep_outputs else
                                           "emission off, RF off, disconnected")

    # ============================================================== commands

    def set_emission(self, on: bool, owner: str | None = None) -> None:
        """Switch the laser emission. ON is refused unless connected and the
        interlock reads OK. OFF is always accepted.

        `owner` (a client id) arms the LOST-CLIENT GUARD: the remote GUI's
        client sends it and then pings; if that client stays silent for
        hardware.client_timeout_s, the worker switches emission off. ON without
        an owner (scan routine, console, the in-process GUI) has no guard --
        and TAKES OVER ownership, so a scan that switches the laser on in its
        routine is not cut when a GUI that switched it on earlier closes."""
        on = bool(on)
        if on:
            if not self._connected:
                raise SafetyError("emission refused: laser not connected")
            with self._lock:
                code = self.backend.read_interlock()
            if code != 2:
                raise SafetyError(f"emission refused: interlock "
                                  f"{INTERLOCK_TEXT.get(code, code)}")
        # OFF: forget the request FIRST, so even if the write fails the brain
        # never believes emission is wanted. ON: remember it only once the
        # laser has taken the command, so a failed write leaves "off".
        if not on:
            self._emission = False
            self._owner = None
        if self._connected:
            with self._lock:
                self.backend.set_emission(on)
        if on:
            # owner and its clock BEFORE the emission flag: the worker's guard
            # reads them without a lock, and must never pair a fresh "on" with
            # the previous owner's stale heartbeat time
            self._owner_seen = time.monotonic()
            self._owner = owner or None
        self._emission = on
        self._emit("warn" if on else "info",
                   "EMISSION ON requested (class 4 laser)" if on else "emission OFF")

    def emission_off(self) -> None:
        """Emission OFF. Same as set_emission(False); a name of its own
        because over the wire it is the SAFETY verb a viewer may always send
        (net/service.py, control) -- set_emission can also switch it ON."""
        self.set_emission(False)

    def touch(self, client: str | None) -> None:
        """A client said something (the service calls this on EVERY command,
        `ping` included). Only the owner's words feed the lost-client guard:
        another client being alive does not prove the owner still is."""
        if client and client == self._owner:
            self._owner_seen = time.monotonic()

    def reset_interlock(self) -> None:
        """Acknowledge a closed interlock. Does NOT switch emission on."""
        if self._connected:
            with self._lock:
                self.backend.reset_interlock()
        self._emit("info", "interlock reset sent")

    def set_power(self, pct: float) -> None:
        value = self._clamped_power(float(pct), announce=True)
        self._power = value
        if self._connected:
            with self._lock:
                self.backend.set_power(value)

    def set_rf(self, on: bool) -> None:
        self._rf = bool(on)
        if self._connected:
            with self._lock:
                self.backend.set_rf(self._rf)
        self._emit("info", f"AOTF RF {'ON' if self._rf else 'OFF'}")

    def set_filter(self, name) -> None:
        """Drive another crystal. The RF is switched off while the crystal
        changes and restored afterwards; line wavelengths outside the new range
        are clamped into it (and announced)."""
        names = self.filter_names()
        key = str(name).strip().lower()
        by_name = key in [n.lower() for n in names]
        # a number is accepted as an INDEX into the table, but only a valid one:
        # "7" must not silently fall back to crystal 0
        by_index = key.isdigit() and 0 <= int(key) < len(names)
        if not (by_name or by_index):
            raise ValueError(f"unknown filter {name!r} (have {', '.join(names)})")
        idx = self._filter_index(name)
        # a crystal change moves every line's range and blips the RF: no
        # sweep may walk through it (stopped BEFORE the lock, see SoftRamp)
        if self._ramp.stop():
            self._emit("info", "wavelength sweep stopped by a crystal change")
        with self._lock:
            self._apply_filter_locked(idx, announce=True, switch=True)

    def set_wavelength(self, line: int, nm: float) -> None:
        """Line is 1-based (1..8), like the channels on the RF driver.

        A set of the line that is being SWEPT takes it over: the sweep is
        stopped first (BEFORE the lock its step may be waiting for). A set of
        another line leaves the sweep running -- it does not touch that line."""
        i = self._line_index(line)
        if self._ramp.running and i == self._ramp_line and self._ramp.stop():
            self._emit("info", f"wavelength sweep of line {i + 1} stopped by a set")
        lo, hi = self._range
        value, clamped = _clamp(float(nm), lo, hi)
        self._wl[i] = value
        if self._connected:
            with self._lock:
                self.backend.set_wavelength(i, value)
        if clamped:
            self._emit("warn", f"line {i + 1}: wavelength clamped to {value:g} nm "
                               f"({self.active_filter()} crystal {lo:g}..{hi:g} nm)")

    def set_amplitude(self, line: int, pct: float) -> None:
        i = self._line_index(line)
        value, clamped = _clamp(float(pct), 0.0, self.cfg.limits.amplitude_max_pct)
        self._amp[i] = value
        if self._connected:
            with self._lock:
                self.backend.set_amplitude(i, value)
        if clamped:
            self._emit("warn", f"line {i + 1}: amplitude clamped to {value:g} % "
                               f"(limit 0..{self.cfg.limits.amplitude_max_pct:g})")

    def set_line(self, line: int, nm: float, pct: float) -> None:
        self.set_wavelength(line, nm)
        self.set_amplitude(line, pct)

    # ======================================================= wavelength SWEEP

    def ramp_rate_limits(self) -> tuple[float, float]:
        """(min, max) sweep pace in nm/s, from cfg.limits."""
        lim = self.cfg.limits
        lo = max(1e-6, float(lim.ramp_rate_min_nm_per_s))
        return lo, max(lo, float(lim.ramp_rate_max_nm_per_s))

    def ramp_wavelength(self, line: int, nm: float, rate_nm_per_s: float) -> int:
        """Sweep line `line` (1-based) from its present wavelength to `nm` at
        `rate_nm_per_s`; returns the sweep's number. Target clamped to the
        active crystal, rate to the limits (both warned). Emission, RF, power
        and amplitude are NOT touched: a sweep of a dark line is a sweep of a
        dark line (the caller switches the light, e.g. in a scan routine)."""
        i = self._line_index(line)
        rate = abs(float(rate_nm_per_s))
        if not rate > 0 or rate != rate:
            raise ValueError("rate must be > 0")
        target = float(nm)
        if target != target or target in (float("inf"), float("-inf")):
            raise ValueError(f"wavelength must be a finite number, got {nm!r}")
        lo, hi = self._range
        value, clamped = _clamp(target, lo, hi)
        r, rclamped = _clamp(rate, *self.ramp_rate_limits())
        # the line is switched only with no walk running: stop, THEN point the
        # ramp at the new line, THEN start (start() would stop it again, but by
        # then the old walk must not write into the new line)
        self._ramp.stop()
        self._ramp_line = i
        rid = self._ramp.start(value, r)
        if clamped or rclamped:
            self._emit("warn", f"line {i + 1}: sweep clamped to {value:g} nm at "
                               f"{r:g} nm/s ({self.active_filter()} {lo:g}..{hi:g} nm)")
        self._emit("info", f"line {i + 1}: wavelength sweep -> {value:g} nm at {r:g} nm/s")
        return rid

    def ramp_stop(self) -> bool:
        """End a sweep where it is. True if one was running. (A SAFETY verb
        over the wire: it only stops.)"""
        was = self._ramp.stop()
        if was:
            i = self._ramp_line
            self._emit("info", f"line {i + 1}: wavelength sweep stopped at "
                               f"{self._wl[i]:g} nm")
        return was

    def _ramp_step(self, nm: float) -> None:
        """One step of the sweep, on the sweep's thread: write the line's
        wavelength register and record ALL lines' commanded wavelengths with
        the moment the write returned. Quiet: no event per step."""
        i = self._ramp_line
        with self._lock:
            if self._connected:
                self.backend.set_wavelength(i, float(nm))
            self._wl[i] = float(nm)
            row = list(self._wl)
        self.recorder.append(time.time(), row)

    def _ramp_done(self, rid: int, reason: str) -> None:
        i = self._ramp_line
        if reason == "done":
            self._emit("info", f"line {i + 1}: wavelength sweep done at {self._wl[i]:g} nm")
        elif reason.startswith("error"):
            self._emit("error", f"line {i + 1}: wavelength sweep ended: {reason}")

    # the stream verbs: every wavelength the sweep sent (group "ramp")
    def stream_start(self) -> int:
        sid = self.recorder.start()
        # the PRESENT wavelengths as the first row: a fly row's lead-in, at
        # rest, needs a value to look up before the walk sends its first step
        self.recorder.append(time.time(), list(self._wl))
        return sid

    def stream_read(self) -> dict:
        if not self._ramp.running:
            # at rest nothing changes, but the record must go on covering time
            # (a lagging detector's tail is looked up later)
            self.recorder.append(time.time(), list(self._wl))
        return self.recorder.read()

    def stream_stop(self) -> dict:
        if not self._ramp.running:
            self.recorder.append(time.time(), list(self._wl))
        return self.recorder.stop()

    # ================================================================ status

    def status(self) -> Status:
        """The last snapshot the worker built. Never touches hardware.

        The sweep's fields are laid over it LIVE (in memory, no hardware): the
        snapshot is only rebuilt at poll_hz, and a fly scan waiting for the
        end of a sweep should not wait a poll period for nothing."""
        r = self._ramp.status()
        st = self._status
        live = {"ramping": r["ramping"], "ramp_id": r["ramp_id"],
                "ramp_line": self._ramp_line + 1 if r["ramp_id"] else 0,
                "ramp_target_nm": r["ramp_target"] or 0.0,
                "ramp_rate_nm_per_s": r["ramp_rate"] or 0.0}
        if all(getattr(st, k) == v for k, v in live.items()):
            return st                        # nothing to lay over: the snapshot itself
        return replace(st, **live)           # a COPY: the snapshot is never edited

    # ============================================================== settings

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config edits self.cfg in place (or the Settings
        dialog does). Never turns emission on, and writes ONLY what the edit
        actually changes on the laser:
          * a value the new limits no longer allow is clamped (and announced);
          * a changed watchdog is written;
          * a crystal-table edit that changes the active crystal's number
            re-switches (otherwise no RF blip);
          * a changed PRESET (group "startup") is sent through its setter.
        An unchanged Settings > Apply therefore writes nothing at all."""
        self._sanitise_limits()
        self._filter = min(self._filter, max(len(self.filter_names()) - 1, 0))
        # a settings change may move the crystal's range under a running sweep
        if self._ramp.stop():
            self._emit("info", "wavelength sweep stopped by a settings change")
        with self._lock:
            p = self._clamped_power(self._power, announce=True)
            if p != self._power:
                self._power = p
                if self._connected:
                    self.backend.set_power(p)
            if self._connected:
                self._arm_watchdog_locked()
            code = self._crystal_code(self._filter)
            self._apply_filter_locked(self._filter, announce=True,
                                      switch=code != self._applied_code)
            for i in range(N_LINES):
                a, clamped = _clamp(self._amp[i], 0.0, self.cfg.limits.amplitude_max_pct)
                if clamped:
                    self._emit("warn", f"line {i + 1}: amplitude clamped to {a:g} %")
                    self._amp[i] = a
                    if self._connected:
                        self.backend.set_amplitude(i, a)
        self._apply_changed_presets()

    def _apply_changed_presets(self) -> None:
        """Send the presets someone CHANGED since they were last seen (at
        construction or at the previous apply). Line lists go line by line, so
        editing line 3 does not re-send lines 1..8."""
        new = dict(vars(self.cfg.startup))
        old, self._presets_seen = self._presets_seen, new
        if new["filter"] != old.get("filter"):
            self.set_filter(new["filter"])
        if new["power_pct"] != old.get("power_pct"):
            self.set_power(float(new["power_pct"]))
        wl_n = C.floats(new["wavelengths_nm"], N_LINES, 650.0)
        wl_o = C.floats(old.get("wavelengths_nm", ""), N_LINES, float("nan"))
        am_n = C.floats(new["amplitudes_pct"], N_LINES, 0.0)
        am_o = C.floats(old.get("amplitudes_pct", ""), N_LINES, float("nan"))
        for i in range(N_LINES):
            if wl_n[i] != wl_o[i]:
                self.set_wavelength(i + 1, wl_n[i])
            if am_n[i] != am_o[i]:
                self.set_amplitude(i + 1, am_n[i])

    # ============================================================= internals

    def _line_index(self, line) -> int:
        i = int(line) - 1
        if not 0 <= i < N_LINES:
            raise ValueError(f"line must be 1..{N_LINES}, got {line}")
        return i

    def _sanitise_limits(self) -> None:
        """The limits themselves arrive over the wire (set_config) or from a
        hand-edited .ini, so they are checked too: both registers are percent
        of full scale, and a ceiling above 100 % or below the floor would make
        the 'safety envelope' meaningless."""
        lim = self.cfg.limits
        fixed = []
        for name in ("power_min_pct", "power_max_pct", "amplitude_max_pct"):
            v = float(getattr(lim, name))
            c = min(max(v, 0.0), 100.0)
            if c != v:
                setattr(lim, name, c)
                fixed.append(f"{name}={c:g}")
        if lim.power_min_pct > lim.power_max_pct:
            lim.power_min_pct = lim.power_max_pct
            fixed.append(f"power_min_pct={lim.power_min_pct:g}")
        if fixed:
            self._emit("warn", "limits out of range, corrected: " + ", ".join(fixed))

    def _clamped_power(self, pct: float, announce: bool) -> float:
        lim = self.cfg.limits
        value, clamped = _clamp(pct, lim.power_min_pct, lim.power_max_pct)
        if clamped and announce:
            self._emit("warn", f"power level clamped to {value:g} % "
                               f"(limit {lim.power_min_pct:g}..{lim.power_max_pct:g})")
        return value

    def _apply_filter_locked(self, idx: int, announce: bool, switch: bool = True) -> None:
        """Select crystal `idx`, learn its range, re-clamp every line into it.
        Caller holds self._lock.

        switch=False only re-reads the range and re-clamps (a config change
        that did not change which crystal is meant), so the RF is not blipped,
        and only the lines that the clamp actually MOVED are written.

        ORDER MATTERS on the real hardware: the SELECT's RF switch must not
        move under RF power (SDK manual 6.10), so RF goes off first and comes
        back only after the new crystal is in place. If the switch FAILS (the
        crystal sits in the other housing and the cable must be moved), the
        brain keeps the old crystal, and the RF stays OFF: the lines would
        otherwise come out of a crystal nobody meant.
        """
        old = self._filter
        changed = idx != old
        rng = self._config_range(idx)
        if self._connected and switch:
            if self._rf:
                self.backend.set_rf(False)          # no RF while the crystal changes
            try:
                self.backend.select_crystal(self._crystal_code(idx))
            except Exception:
                self._rf = False
                raise
            self._applied_code = self._crystal_code(idx)
        self._filter = idx
        if self._connected:
            hw = self.backend.read_crystal_range()
            if hw:
                rng = hw                            # the driver knows best
        self._range = rng
        lo, hi = rng
        for i in range(N_LINES):
            v, clamped = _clamp(self._wl[i], lo, hi)
            if clamped and announce and self._amp[i] > 0:
                self._emit("warn", f"line {i + 1}: {self._wl[i]:g} nm is outside "
                                   f"{self.active_filter()} ({lo:g}..{hi:g} nm), "
                                   f"moved to {v:g} nm")
            moved = v != self._wl[i]
            self._wl[i] = v
            # after a crystal switch every line is re-sent (an explicit user
            # action on a new crystal); otherwise only what the clamp moved
            if self._connected and (switch or moved):
                self.backend.set_wavelength(i, v)
        if self._connected and switch and self._rf:
            self.backend.set_rf(True)
        if announce and changed:
            self._emit("info", f"filter {self.active_filter()} ({lo:g}..{hi:g} nm)")

    def _worker(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._check_owner()
            self._poll_once()
            period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
            # time.sleep, not Event.wait: a timed wait rounds up to the 15.6 ms
            # Windows tick (gotcha #34); the stop flag is checked every loop.
            remaining = period - (time.monotonic() - t0)
            end = time.monotonic() + max(0.0, remaining)
            while not self._stop.is_set() and time.monotonic() < end:
                time.sleep(0.02)

    def _check_owner(self) -> None:
        """Lost-client guard: the client that switched emission on (with an
        owner id) has been silent for longer than hardware.client_timeout_s
        -> emission OFF. RF and everything else are left alone (not dangerous
        without emission, and a reconnecting GUI finds them as they were)."""
        timeout = float(self.cfg.hardware.client_timeout_s)
        if not self._owner or timeout <= 0 or not self._emission:
            return
        silent = time.monotonic() - self._owner_seen
        if silent <= timeout:
            return
        self._owner = None
        self._emission = False
        try:
            with self._lock:
                self.backend.set_emission(False)
        except Exception as exc:                       # the poll will show hw_error
            self._emit("error", f"lost-client guard: emission OFF failed: {exc}")
        self._emit("warn", f"the client that switched emission on has been silent "
                           f"for {silent:.1f} s (> {timeout:g} s): emission OFF")

    def _poll_once(self) -> None:
        """Read the hardware and build a NEW Status. The only writer of _status."""
        if not self._connected:
            return
        st = Status(connected=True, idn=self._idn)
        try:
            with self._lock:
                code = int(self.backend.read_interlock())
                emitting = bool(self.backend.read_emission())
                st.status_bits = int(self.backend.read_status_bits())
                st.power_pct = float(self.backend.read_power())
                st.inlet_temp_C = float(self.backend.read_inlet_temp())
                st.rf_on = bool(self.backend.read_rf())
                st.crystal_temp_C = float(self.backend.read_crystal_temp())
                st.crystal = int(self.backend.read_crystal())
                st.wavelength_nm = [float(self.backend.read_wavelength(i))
                                    for i in range(N_LINES)]
                st.amplitude_pct = [float(self.backend.read_amplitude(i))
                                    for i in range(N_LINES)]
                # copy the brain's desired state under the SAME lock the
                # setters hold, so one frame never mixes an old crystal with
                # a new range (the snapshot itself is still only built here)
                wanted = (self._emission, self._power, self._rf, self._filter,
                          self._range, list(self._wl), list(self._amp))
        except Exception as exc:                      # never let the worker die
            st = Status(**{**self._status.__dict__})
            st.hw_error = str(exc)
            st.emission_state = "error"
            self._status = st
            return
        _em, power_w, rf_w, filt_w, range_w, wl_w, amp_w = wanted
        # the interlock dropped while we wanted emission: forget the request,
        # so closing the door again does not bring the beam back by itself
        if self._emission and code != 2:
            self._emission = False
            self._owner = None
            self._emit("warn", f"interlock {INTERLOCK_TEXT.get(code, code)}: "
                               f"emission request cleared")
        st.interlock_code = code
        st.interlock = INTERLOCK_TEXT.get(code, str(code))
        st.interlock_ok = code == 2
        st.emission_on = emitting
        st.emission_set = self._emission
        if emitting:
            st.emission_state = "on"
        elif not st.interlock_ok:
            st.emission_state = "interlock"
        elif st.emission_set:
            st.emission_state = "starting"
        else:
            st.emission_state = "off"
        names = self.filter_names()
        st.power_set_pct = power_w
        st.rf_set = rf_w
        st.filter = names[filt_w] if 0 <= filt_w < len(names) else None
        st.filter_min_nm, st.filter_max_nm = range_w
        st.wavelength_set_nm = wl_w
        st.amplitude_set_pct = amp_w
        st.emission_guarded = bool(self._owner) and st.emission_set
        self._status = st

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
