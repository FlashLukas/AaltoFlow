"""SuperK: the small "brain" between the wire and the backend.

A supercontinuum laser with an AOTF filter is a SET-AND-FORGET instrument: you
command emission, a power level, which crystal, and up to 8 lines (wavelength +
RF amplitude each), and the hardware holds them. So there is no control loop
here. What there IS, because this is a CLASS 4 LASER, is safety logic:

  * emission is never switched on by starting the service;
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
from dataclasses import dataclass, field

from . import config as C
from .backends.base import SupercontinuumBackend
from .config import Config, N_LINES

INTERLOCK_TEXT = {0: "open", 1: "needs reset", 2: "OK"}


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
    emission_state: str = "off"        # off | starting | on | interlock | error
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
    filter: str = ""
    filter_min_nm: float = 0.0
    filter_max_nm: float = 0.0
    crystal_temp_C: float = 0.0
    wavelength_set_nm: list = field(default_factory=lambda: [0.0] * N_LINES)
    wavelength_nm: list = field(default_factory=lambda: [0.0] * N_LINES)
    amplitude_set_pct: list = field(default_factory=lambda: [0.0] * N_LINES)
    amplitude_pct: list = field(default_factory=lambda: [0.0] * N_LINES)
    hw_error: str = ""


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
        self._applied_code = None               # crystal number last switched to
        self._sanitise_limits()
        self._wl = C.floats(s.wavelengths_nm, N_LINES, 0.0)
        self._amp = C.floats(s.amplitudes_pct, N_LINES, 0.0)
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
        """Connect and push the safe start-up state. Emission is NOT switched on."""
        hw = self.cfg.hardware
        with self._lock:
            self.backend.open()
            self._connected = True
            self._idn = self.backend.identify()
            if hw.emission_off_on_start:
                self.backend.set_emission(False)
            self.backend.set_rf(False)
            self.backend.set_watchdog(int(hw.watchdog_s))
            self._power = self._clamped_power(self._power, announce=True)
            self.backend.set_power(self._power)
            try:
                self._apply_filter_locked(self._filter, announce=False, switch=True)
            except Exception as exc:
                # The start-up crystal cannot be reached (e.g. it sits in the
                # other SELECT housing and the RF cable is not there). Do not
                # refuse to start: adopt the crystal the driver DOES reach, if
                # it is in the table, and say so. RF is off either way.
                self._emit("warn", f"start-up crystal {self.active_filter()}: {exc}")
                self._adopt_connected_crystal_locked()
            amax = self.cfg.limits.amplitude_max_pct
            for i in range(N_LINES):
                self._amp[i] = _clamp(self._amp[i], 0.0, amax)[0]
                self.backend.set_wavelength(i, self._wl[i])
                self.backend.set_amplitude(i, self._amp[i])
            # if emission was left on (front panel) and we did not switch it
            # off, adopt it so the GUI and a scan see the truth
            self._emission = bool(self.backend.read_emission())
        self._emit("info", f"connected: {self._idn or 'SuperK'} (emission "
                           f"{'ON' if self._emission else 'off'}, RF off)")
        self._poll_once()
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="superk-poll",
                                        daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """RF off, emission off, disconnect. Safe to call more than once / on a crash."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            try:
                if self._connected:
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
                    self.backend.close()
                finally:
                    was = self._connected
                    self._connected = False
                    st = Status(**{**self._status.__dict__})
                    st.connected = False
                    self._status = st
                    if was:
                        self._emit("info", "emission off, RF off, disconnected")

    # ============================================================== commands

    def set_emission(self, on: bool) -> None:
        """Switch the laser emission. ON is refused unless connected and the
        interlock reads OK. OFF is always accepted."""
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
        if self._connected:
            with self._lock:
                self.backend.set_emission(on)
        self._emission = on
        self._emit("warn" if on else "info",
                   "EMISSION ON requested (class 4 laser)" if on else "emission OFF")

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
        with self._lock:
            self._apply_filter_locked(idx, announce=True, switch=True)

    def set_wavelength(self, line: int, nm: float) -> None:
        """Line is 1-based (1..8), like the channels on the RF driver."""
        i = self._line_index(line)
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

    # ================================================================ status

    def status(self) -> Status:
        """The last snapshot the worker built. Never touches hardware."""
        return self._status

    # ============================================================== settings

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp everything to the (possibly new) limits / filter table.
        Called after set_config edits self.cfg in place. Never turns emission on."""
        self._sanitise_limits()
        self._power = self._clamped_power(self._power, announce=True)
        self._filter = min(self._filter, max(len(self.filter_names()) - 1, 0))
        with self._lock:
            if self._connected:
                self.backend.set_power(self._power)
                self.backend.set_watchdog(int(self.cfg.hardware.watchdog_s))
            # switch the RF switch only if the crystal MEANT by the active
            # entry changed (someone edited the crystal table); otherwise just
            # re-read the range and re-clamp, without blipping the RF
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

    # ============================================================= internals

    def _line_index(self, line) -> int:
        i = int(line) - 1
        if not 0 <= i < N_LINES:
            raise ValueError(f"line must be 1..{N_LINES}, got {line}")
        return i

    def _adopt_connected_crystal_locked(self) -> None:
        """Point the brain at whichever table entry the RF driver is really
        connected to (used when the wanted crystal could not be reached)."""
        try:
            code = int(self.backend.read_crystal())
        except Exception:
            return
        n = len(self.filter_names())
        codes = C.ints(self.cfg.filters.crystal, n, 1) if n else []
        if code in codes:
            self._applied_code = code
            self._apply_filter_locked(codes.index(code), announce=True, switch=False)
            self._emit("info", f"using the connected crystal {self.active_filter()}")

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
        that did not change which crystal is meant), so the RF is not blipped.

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
            self._wl[i] = v
            if self._connected:
                self.backend.set_wavelength(i, v)
        if self._connected and switch and self._rf:
            self.backend.set_rf(True)
        if announce and changed:
            self._emit("info", f"filter {self.active_filter()} ({lo:g}..{hi:g} nm)")

    def _worker(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._poll_once()
            period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
            # time.sleep, not Event.wait: a timed wait rounds up to the 15.6 ms
            # Windows tick (gotcha #34); the stop flag is checked every loop.
            remaining = period - (time.monotonic() - t0)
            end = time.monotonic() + max(0.0, remaining)
            while not self._stop.is_set() and time.monotonic() < end:
                time.sleep(0.02)

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
        st.filter = names[filt_w] if 0 <= filt_w < len(names) else ""
        st.filter_min_nm, st.filter_max_nm = range_w
        st.wavelength_set_nm = wl_w
        st.amplitude_set_pct = amp_w
        self._status = st

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
