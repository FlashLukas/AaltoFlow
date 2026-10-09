"""The Synthesizer: the small "brain" between the wire and the backend.

A CW signal generator needs no control loop, so this is a set-and-forget brain:

  * hold the DESIRED signal (frequency, power, phase, reference, RF on/off),
  * CLAMP every request to the intersection of YOUR safety envelope (cfg
    limits) and the UNIT's own range (read at connect), and announce a clamp as
    a warning event, so nothing silently drives the sample harder than asked,
  * push the accepted value to whichever backend is wired in (sim or real),
  * keep a status SNAPSHOT that a worker thread rebuilds from the instrument's
    READ-BACK, so a scan waiting for "echo" waits for the box, not for our
    memory of what we asked.

Start-up ADOPTS (Lukas's rule, 2026-09-27: "all modules should read the
instrument state on startup, not to change anything"). start() reads RF on/off,
frequency, power, phase and reference from the unit and takes them as the
desired values; it writes nothing that changes the unit. If the unit sits
outside your limits it is LEFT there with a warning -- the next setter clamps.
The config's `signal` preset and the buzzer/display preferences are sent only
when they CHANGE (apply_config), never at start. Shutdown still turns RF off.

Threads (gotcha #1 in docs/DEVELOPER_NOTES.md)
----------------------------------------------
Two threads touch this object: the service's command thread (setters) and our
own poll thread (read-back). Rules:
  * every backend call happens under ONE lock (`self._io`), because a serial
    line or TCP socket carries one question and one answer at a time;
  * setters change brain ATTRIBUTES (the desired values) and the hardware;
    they never write into the snapshot;
  * the poll thread builds a NEW Status object each cycle and swaps it in with
    a single assignment, so a reader never sees half an update;
  * status() only returns that snapshot: it never touches the hardware, so a
    slow or unplugged instrument cannot stall the publisher or a GUI.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, replace

from . import vernier_cal
from .backends.base import MicrowaveSource
from .config import Config, REFERENCES
from .softramp import SoftRamp


@dataclass
class Status:
    """One snapshot of the generator, for status() and the wire."""

    rf_on: bool = False
    frequency_Hz: float = 0.0
    power_dBm: float = 0.0
    phase_deg: float = 0.0
    vernier: int = 0                # fine power trim, raw counts (no unit)
    reference: str = "auto"
    ext_ref_detected: bool = False
    usb_volts: float = 0.0
    connected: bool = False
    has_phase: bool = False
    has_vernier: bool = False
    # FINE POWER (vernier_cal.py): the vernier fills the attenuator's 0.5 dB
    # gaps and power_dBm is attenuator + vernier, the level delivered
    fine_power: bool = False
    attenuator_dBm: float = 0.0     # the step attenuator alone (POWER?)
    # POWER CALIBRATION (vernier_cal.Calibration): True when a measured
    # calibration of this unit is loaded AND in use (fine power on), i.e. the
    # published power_dBm includes the measured step errors
    power_calibrated: bool = False
    power_calibration: str = ""     # "file, measured date, f range"; "" = none loaded
    idn: str = ""
    hw_error: str = ""
    # the effective (cfg AND instrument) envelope, published so a client can
    # draw the right slider without a second round trip
    freq_min_Hz: float = 0.0
    freq_max_Hz: float = 0.0
    power_min_dBm: float = 0.0
    power_max_dBm: float = 0.0
    polls: int = 0                  # increments every read-back cycle
    # The FREQUENCY SWEEP (ramp_frequency, for fly scans, 2026-10-09). Live
    # values of the software ramp, laid over the snapshot by status() (they
    # are in memory: no hardware is read for them). ramp_id = the newest
    # sweep started; "ramp_id >= mine and not ramping" = my sweep is over.
    ramping: bool = False
    ramp_id: int = 0
    ramp_target_Hz: float = 0.0
    ramp_rate_Hz_per_s: float = 0.0


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Synthesizer:
    def __init__(self, backend: MicrowaveSource, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        s = self.cfg.signal
        # the DESIRED signal. Before start() these are only placeholders from
        # the preset (shown while offline); start() replaces them with what
        # the unit is actually doing (adopt, never push).
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        # The VERNIER is not part of the preset: it is always adopted from the
        # unit, and only a set_vernier changes it.
        self._vernier = 0
        # the step attenuator's own setting (fine power splits _power into
        # _att + vernier); adopted at start, kept by every power/frequency set
        self._att = float(s.power_dBm)
        # The measured POWER CALIBRATION (vernier_cal.Calibration) or None.
        # Loaded at start from cfg.hardware.power_calibration; a RELATIVE path
        # is looked up in `calibration_dir`, which run_service.py sets to the
        # module folder. None (the default, and what the tests and a local GUI
        # simulation get) = relative paths are not loaded at all, so a unit's
        # calibration file lying in the module folder can never change what a
        # test sees.
        self._cal: vernier_cal.Calibration | None = None
        self.calibration_dir: str | None = None
        self._cal_out_of_range = False      # "outside the table" said once
        self._reference = s.reference if s.reference in REFERENCES else "auto"
        self._rf_on = False                 # adopted at start; never switched ON by us
        # The config values the brain has already acted on. apply_config()
        # compares against this, so only a value the user CHANGED is sent --
        # pressing Apply in Settings (which sends the whole config) must not
        # overwrite the adopted instrument state with the stale preset.
        self._seen = self._cfg_snapshot()
        # what the unit itself can do; unknown (None) until start()
        self._unit_freq: tuple[float, float] | None = None
        self._unit_power: tuple[float, float] | None = None
        self._has_phase = False
        self._has_vernier = False
        self._idn = ""
        self._connected = False

        self._io = threading.RLock()        # guards every backend call
        # Guards the DESIRED signal (_freq, _power, _att, _vernier) against the
        # sweep thread: a power set during a frequency sweep and a sweep step
        # must not interleave their attenuator/vernier split halfway.
        self._sig = threading.RLock()
        # THE FREQUENCY SWEEP (suite_common/softramp.py, copied as softramp.py).
        # The SERVICE walks the frequency: the SG12000L has no sweep of its
        # own that a fly scan could follow. Every step is one FREQ:CW command
        # (plus the fine-power re-split when the level must stay put).
        self._ramp = SoftRamp(self._ramp_step, lambda: self._freq,
                              limits=lambda: (self.limits()["freq_min_Hz"],
                                              self.limits()["freq_max_Hz"]),
                              dt_s=float(getattr(self.cfg.hardware, "ramp_dt_s", 0.05)),
                              on_done=self._ramp_done, channel="frequency",
                              name="dssg-sweep")
        self._stop = threading.Event()
        self._poke = threading.Event()      # "read back NOW": set after a command
        self._poll_t: threading.Thread | None = None
        self._status = self._offline_snapshot()
        self._last_err_check = 0.0
        self._last_reopen = -1e9            # see REOPEN_S / _poll_once
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- the effective envelope ------------------------------------------

    def limits(self) -> dict:
        """cfg limits intersected with the unit's own range. Read by describe,
        info, the GUI and every setter -- one definition, no copies."""
        lim = self.cfg.limits
        f_lo, f_hi = lim.freq_min_Hz, lim.freq_max_Hz
        p_lo, p_hi = lim.power_min_dBm, lim.power_max_dBm
        if self._unit_freq:
            f_lo, f_hi = max(f_lo, self._unit_freq[0]), min(f_hi, self._unit_freq[1])
        if self._unit_power:
            p_lo, p_hi = max(p_lo, self._unit_power[0]), min(p_hi, self._unit_power[1])
        # A config that does not overlap the unit at all would give lo > hi;
        # collapse to the lower bound rather than produce an inverted range.
        f_hi, p_hi = max(f_hi, f_lo), max(p_hi, p_lo)
        return {"freq_min_Hz": f_lo, "freq_max_Hz": f_hi,
                "power_min_dBm": p_lo, "power_max_dBm": p_hi,
                "phase_min_deg": lim.phase_min_deg, "phase_max_deg": lim.phase_max_deg,
                "vernier_min": int(lim.vernier_min), "vernier_max": int(lim.vernier_max)}

    def has_phase(self) -> bool:
        return self._has_phase

    def has_vernier(self) -> bool:
        return self._has_vernier

    def fine_power(self) -> bool:
        """True when the vernier fills the attenuator's steps (cfg
        hardware.fine_power AND a unit that has a vernier). Before connect we
        do not know the unit yet: assume it can, like phase and vernier."""
        if not bool(getattr(self.cfg.hardware, "fine_power", False)):
            return False
        return self._has_vernier or not self._connected

    def _delivered(self, attenuator: float, counts: int, freq: float) -> float:
        """The level we believe comes out: attenuator (+ its measured step
        error, with a calibration) + the vernier's share (rounded to 0.01 dB:
        the model is not better than that)."""
        return round(vernier_cal.delivered(attenuator, counts, freq, self._cal), 2)

    # ---- the power calibration ----------------------------------------------

    def power_calibration(self):
        """The loaded vernier_cal.Calibration, or None."""
        return self._cal

    def calibration_path(self) -> str | None:
        """Where cfg.hardware.power_calibration points, or None when it is
        empty, or relative with no calibration_dir to resolve it against."""
        p = str(getattr(self.cfg.hardware, "power_calibration", "") or "").strip()
        if not p:
            return None
        if os.path.isabs(p):
            return p
        if self.calibration_dir is None:
            return None
        return os.path.join(self.calibration_dir, p)

    def load_power_calibration(self) -> None:
        """(Re)load the calibration file. Never raises: a missing file means
        "nominal steps", a broken one is a WARNING and no calibration -- a bad
        file must not keep the generator from starting."""
        self._cal = None
        self._cal_out_of_range = False
        path = self.calibration_path()
        if path is None:
            return
        if not os.path.isfile(path):
            self._emit("info", f"no power calibration ({os.path.basename(path)} not "
                               f"found): fine power uses the nominal attenuator steps")
            return
        try:
            self._cal = vernier_cal.Calibration.load(path)
        except Exception as exc:
            self._emit("warn", f"power calibration {os.path.basename(path)} NOT used: "
                               f"{type(exc).__name__}: {exc}")
            return
        self._emit("info", f"power calibration loaded: {self._cal.describe()}"
                   + ("" if self.cfg.hardware.fine_power else
                      " (not used while fine power is off)"))

    def _note_cal_range(self, freq: float) -> None:
        """Say ONCE when the frequency leaves the calibrated range (the
        nearest end values are used there); said again only after it came
        back and left again."""
        cal = self._cal
        if cal is None or not self.fine_power():
            return
        if cal.in_range(freq):
            self._cal_out_of_range = False
        elif not self._cal_out_of_range:
            self._cal_out_of_range = True
            self._emit("warn", f"{freq / 1e6:.3f} MHz is outside the power calibration "
                               f"({cal.freqs_Hz[0] / 1e9:g}-{cal.freqs_Hz[-1] / 1e9:g} GHz): "
                               f"its nearest end values are used")

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, learn what the unit can do, ADOPT what it is
        doing (read only -- nothing is changed), start the read-back thread."""
        # the calibration first: adopting a fine-power setting computes the
        # level it delivers, which depends on it
        self.load_power_calibration()
        try:
            self._connect_and_adopt()
        except Exception:
            # Half-way failures (port opened, then a query timed out) must not
            # leave the COM port / socket held open by a process that is about
            # to exit with a traceback: release it. rf_off=False: we never took
            # control, so the unit is left exactly as we found it.
            self._connected = False
            try:
                with self._io:
                    self.backend.close(rf_off=False)
            except Exception:
                pass
            raise
        self._emit("info", f"connected: {self._idn or 'SG12000L'}; adopted "
                           f"RF {'ON' if self._rf_on else 'off'}, "
                           f"{self._freq / 1e6:.6f} MHz, {self._power:g} dBm"
                           + (f" (vernier {self._vernier:+d})" if self._has_vernier else "")
                           + ", "
                           f"ref {self._reference} (nothing changed)")
        self._warn_if_outside_limits()
        self._seen = self._cfg_snapshot()       # the config as it stood at connect
        self._poll_once()                       # a valid snapshot before anyone asks
        self._stop.clear()
        self._poll_t = threading.Thread(target=self._poller, name="dssg-poll",
                                        daemon=True)
        self._poll_t.start()

    def _connect_and_adopt(self) -> None:
        """Queries only. The desired values become what the unit reports, so
        the GUI, describe and a scan's first 'echo' all agree with the box."""
        with self._io:
            b = self.backend
            b.open()                            # connects; changes nothing
            self._connected = True
            self._idn = b.idn()
            try:
                self._unit_freq = tuple(b.freq_range())
                self._unit_power = tuple(b.power_range())
            except Exception as exc:            # keep going on the cfg envelope
                self._emit("warn", f"could not read the unit's range: {exc}")
            self._has_phase = bool(b.has_phase())
            self._rf_on = bool(b.read_output())
            self._freq = float(b.read_frequency())
            self._power = self._att = float(b.read_power())
            if self._has_phase:
                self._phase = float(b.read_phase())
            # getattr: a backend written before the vernier existed simply
            # has none (no crash, no control offered)
            self._has_vernier = bool(getattr(b, "has_vernier", lambda: False)())
            if self._has_vernier:
                self._vernier = int(b.read_vernier())
            if self.fine_power() and (self._vernier or self._cal is not None):
                if abs(self._vernier) <= vernier_cal.MAX_FILL_COUNTS:
                    # a fine-power setting left by us (or alike): adopt the
                    # level it makes, not just the attenuator's (with a
                    # calibration that includes the step's measured error,
                    # also at vernier 0)
                    self._power = self._delivered(self._att, self._vernier, self._freq)
                else:
                    # a big manual trim: its dB is outside the fine-power model
                    self._emit("warn", f"vernier is at {self._vernier:+d} counts (a manual "
                                       f"trim): the power read-back ignores it until "
                                       f"the next power or frequency set resets it")
            ref = b.read_reference()
            if ref in REFERENCES:
                self._reference = ref

    def _warn_if_outside_limits(self) -> None:
        """The unit may have been left beyond YOUR envelope (e.g. +8 dBm from
        the front panel with a +5 dBm ceiling). Adopting means we do not fix
        that behind your back -- but we say so; the next set_* is clamped."""
        lim = self.limits()
        checks = [("frequency", self._freq / 1e6, lim["freq_min_Hz"] / 1e6,
                   lim["freq_max_Hz"] / 1e6, "MHz"),
                  ("power", self._power, lim["power_min_dBm"],
                   lim["power_max_dBm"], "dBm")]
        if self._has_phase:
            checks.append(("phase", self._phase, lim["phase_min_deg"],
                           lim["phase_max_deg"], "deg"))
        if self._has_vernier:
            checks.append(("vernier", self._vernier, lim["vernier_min"],
                           lim["vernier_max"], "counts"))
        for name, v, lo, hi, unit in checks:
            if not lo <= v <= hi:
                self._emit("warn", f"the unit is at {name} {v:g} {unit}, outside "
                                   f"your limits {lo:g}..{hi:g} {unit}: left as "
                                   f"it is (the next set_{name} is clamped)")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """RF off, disconnect. Safe to call more than once / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the port the same, but leave the RF output as
        it is -- the next start adopts it."""
        self._stop.set()
        self._ramp.stop()            # no sweep step may follow the RF off below
        t, self._poll_t = self._poll_t, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        was = self._connected
        try:
            if was and not keep_outputs:
                with self._io:
                    self.backend.set_output(False)
        except Exception as exc:
            self._emit("error", f"RF off failed on shutdown: {exc}")
        finally:
            if not keep_outputs:
                self._rf_on = False
            try:
                with self._io:
                    # rf_off=True: the backend also sends RF off (not on a restart)
                    self.backend.close(rf_off=not keep_outputs)
            finally:
                self._connected = False
                self._status = self._offline_snapshot()
                if was:
                    self._emit("info", "disconnected (RF left as it is)"
                               if keep_outputs else "disconnected (RF off)")

    # ---- commands (each clamps, then pushes) -----------------------------

    def _push(self, fn, *args) -> None:
        """Send one command to the unit (if connected) and ask the poller for
        a fresh read-back right away."""
        if self._connected:
            with self._io:
                fn(*args)
            self._poke.set()

    def set_rf(self, on: bool) -> None:
        self._rf_on = bool(on)
        self._push(self.backend.set_output, self._rf_on)
        self._emit("info", f"RF {'ON' if self._rf_on else 'OFF'}")

    def rf_off(self) -> None:
        """RF off. Same as set_rf(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control)."""
        self.set_rf(False)

    def set_frequency(self, hz: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock below, which a sweep step may be waiting for)
        if self._ramp.stop():
            self._emit("info", "frequency sweep stopped by a frequency set")
        with self._sig:
            self._set_frequency(hz)

    def _set_frequency(self, hz: float) -> None:
        lim = self.limits()
        value, clamped = _clamp(float(hz), lim["freq_min_Hz"], lim["freq_max_Hz"])
        self._freq = value
        self._push(self.backend.set_frequency, value)
        if self.fine_power():
            # the vernier's dB per count -- and, with a calibration, the real
            # level of each attenuator step -- change with frequency: re-split
            # the SAME asked power, so the level stays what was asked
            self._note_cal_range(value)
            att, n = vernier_cal.split(self._power, self._step(), value,
                                       *self._power_lims(), cal=self._cal)
            if att != self._att:
                self._att = att
                self._push(self.backend.set_power, att)
            if n != self._vernier and self._has_vernier:
                self._vernier = n
                self._push(self.backend.set_vernier, n)
        if clamped:
            self._emit("warn", f"frequency clamped to {value / 1e6:.6f} MHz "
                               f"(limit {lim['freq_min_Hz'] / 1e6:g}.."
                               f"{lim['freq_max_Hz'] / 1e6:g} MHz)")
        else:
            self._emit("info", f"frequency = {value / 1e6:.6f} MHz")

    def _step(self) -> float:
        return float(getattr(self.cfg.hardware, "power_step_dB", 0.0) or 0.0)

    def _power_lims(self) -> tuple[float, float]:
        lim = self.limits()
        return lim["power_min_dBm"], lim["power_max_dBm"]

    def set_power(self, dBm: float) -> None:
        with self._sig:
            self._set_power(dBm)

    def _set_power(self, dBm: float) -> None:
        lim = self.limits()
        value, clamped = _clamp(float(dBm), lim["power_min_dBm"], lim["power_max_dBm"])
        if self.fine_power():
            # the attenuator to the nearest step, the vernier for the rest
            # (vernier_cal.py): the level asked for, to ~0.05 dB
            value = round(value, 2)
            self._note_cal_range(self._freq)
            att, n = vernier_cal.split(value, self._step(), self._freq, *self._power_lims(),
                                       cal=self._cal)
            self._power, self._vernier, self._att = value, n, att
            self._push(self.backend.set_power, att)
            if self._has_vernier:
                self._push(self.backend.set_vernier, n)
            got = self._delivered(att, n, self._freq)
            if clamped:
                self._emit("warn", f"power clamped to {value:g} dBm "
                                   f"(limit {lim['power_min_dBm']:g}..{lim['power_max_dBm']:g})")
            elif abs(got - value) > 0.05 + 1e-9:
                # Only with a calibration, at the ends of the range: the
                # nearest real step is further away than the vernier's
                # +-MAX_FILL_COUNTS can bridge (e.g. the lowest step at 10 GHz
                # delivers more than its nominal level). Said, not hidden: a
                # scan waiting for this level would otherwise just time out.
                self._emit("warn", f"power {value:g} dBm cannot be delivered at "
                                   f"{self._freq / 1e6:.3f} MHz: {got:g} dBm is the "
                                   f"nearest (attenuator {att:g} dBm, vernier {n:+d})")
            else:
                self._emit("info", f"power = {value:g} dBm (attenuator {att:g} dBm, "
                                   f"vernier {n:+d}"
                                   + (", calibrated" if self._cal is not None else "") + ")")
            return
        # ROUND to the attenuator step. The SG12000L (firmware V7.84) IGNORES
        # an off-step request -- "POWER -13.75" left it at -20 dBm, and a scan
        # then waited 60 s for a level that never came (lab PC, 2026-10-06).
        # So only values it can make are sent, and the log says when a request
        # was moved.
        asked = value
        step = float(getattr(self.cfg.hardware, "power_step_dB", 0.0) or 0.0)
        if step > 0:
            value = round(round(value / step) * step, 6)
            if value < lim["power_min_dBm"] - 1e-9:     # rounding must not leave the
                value += step                           # safety limits
            elif value > lim["power_max_dBm"] + 1e-9:
                value -= step
        self._power = self._att = value
        self._push(self.backend.set_power, value)
        if clamped:
            self._emit("warn", f"power clamped to {value:g} dBm "
                               f"(limit {lim['power_min_dBm']:g}..{lim['power_max_dBm']:g})")
        elif abs(value - asked) > 1e-9:
            self._emit("info", f"power = {value:g} dBm (asked {asked:g}: the attenuator "
                               f"moves in {step:g} dB steps)")
        else:
            self._emit("info", f"power = {value:g} dBm")

    def set_phase(self, deg: float) -> None:
        if not self._has_phase and self._connected:
            # A refusal, not a silent no-op: a scan over phase on a unit that
            # cannot do it must fail loudly, not measure 100 identical points.
            raise ValueError("this unit has no phase control")
        lim = self.limits()
        value, clamped = _clamp(float(deg), lim["phase_min_deg"], lim["phase_max_deg"])
        self._phase = value
        self._push(self.backend.set_phase, value)
        if clamped:
            self._emit("warn", f"phase clamped to {value:g} deg "
                               f"(limit {lim['phase_min_deg']:g}..{lim['phase_max_deg']:g})")
        else:
            self._emit("info", f"phase = {value:g} deg")

    def set_vernier(self, n) -> None:
        with self._sig:
            self._set_vernier(n)

    def _set_vernier(self, n) -> None:
        """Fine output-power trim, in raw integer counts (no unit).

        The step attenuator only makes 0.5 dB steps; the vernier trims in
        between. The dB per count is NOT documented, so this is deliberately
        a raw number until someone has measured counts -> dB on the unit."""
        if not self._has_vernier and self._connected:
            # refuse loudly, like phase: a scan over the vernier on a unit
            # without one must fail, not measure identical points
            raise ValueError("this unit has no vernier control")
        if self.fine_power():
            raise ValueError("the vernier is used for fine power (set the power in "
                             "0.01 dB instead); switch hardware.fine_power off to "
                             "trim it by hand")
        lim = self.limits()
        # round(), not int(): 2.6 from a GUI or a scan means 3, not 2
        value, clamped = _clamp(int(round(float(n))), lim["vernier_min"],
                                lim["vernier_max"])
        value = int(value)
        self._vernier = value
        self._push(self.backend.set_vernier, value)
        if clamped:
            self._emit("warn", f"vernier clamped to {value:d} "
                               f"(limit {lim['vernier_min']:d}..{lim['vernier_max']:d})")
        else:
            self._emit("info", f"vernier = {value:d}")

    def set_reference(self, mode: str) -> None:
        mode = str(mode).strip().lower()
        if mode not in REFERENCES:
            raise ValueError(f"reference must be one of {', '.join(REFERENCES)}")
        self._reference = mode
        self._push(self.backend.set_reference, mode)
        self._emit("info", f"10 MHz reference = {mode}")

    # ---- the frequency SWEEP (fly scans) ------------------------------------

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float) -> int:
        """Sweep the frequency to `hz` at `rate_Hz_per_s`; returns the sweep's
        number. The target is clamped to the envelope (warned), the rate to
        the configured limits (warned) -- like every setter here.

        Why the record is the COMMANDED frequency (describe: measured false):
        the box could be asked FREQ:CW? on every step, but over the serial
        link that query costs as much as the step itself and would halve the
        steps a second -- and a synthesiser that has acknowledged FREQ:CW is
        at that frequency within its lock time (well under a step), so the
        command IS the frequency to far better than a pixel.
        """
        # VERIFY on the unit: how long one FREQ:CW takes on the serial link
        # (it bounds ramp_dt_s), and whether the output glitches (relocks) on
        # every step -- a lock-in would then see a small dip per step.
        lim = self.limits()
        lo_r = float(self.cfg.limits.ramp_rate_min_Hz_per_s)
        hi_r = float(self.cfg.limits.ramp_rate_max_Hz_per_s)
        rate = abs(float(rate_Hz_per_s))
        if not rate > 0:
            raise ValueError("rate must be > 0")
        r, rclamped = _clamp(rate, lo_r, hi_r)
        value, clamped = _clamp(float(hz), lim["freq_min_Hz"], lim["freq_max_Hz"])
        rid = self._ramp.start(value, r)
        if clamped or rclamped:
            self._emit("warn", f"sweep clamped to {value / 1e6:.6f} MHz at "
                               f"{r / 1e6:g} MHz/s")
        self._emit("info", f"frequency sweep -> {value / 1e6:.6f} MHz at {r / 1e6:g} MHz/s")
        return rid

    def ramp_stop(self) -> bool:
        """End a sweep where it is. True if one was running."""
        was = self._ramp.stop()
        if was:
            self._emit("info", f"frequency sweep stopped at {self._freq / 1e6:.6f} MHz")
        return was

    def _ramp_step(self, hz: float) -> None:
        """One step of the sweep, on the sweep's thread: the frequency (and,
        with fine power, the attenuator/vernier split that keeps the LEVEL
        what was asked -- the vernier's dB per count changes with frequency).
        Quiet (no event per step) and without the read-back poke: a sweep is
        tens of steps a second, the poller keeps its own pace."""
        with self._sig:
            self._freq = float(hz)
            if not self._connected:
                return
            with self._io:
                self.backend.set_frequency(self._freq)
                if self.fine_power():
                    att, n = vernier_cal.split(self._power, self._step(), self._freq,
                                               *self._power_lims(), cal=self._cal)
                    if att != self._att:
                        self._att = att
                        self.backend.set_power(att)
                    if n != self._vernier and self._has_vernier:
                        self._vernier = n
                        self.backend.set_vernier(n)

    def _ramp_done(self, rid: int, reason: str) -> None:
        self._poke.set()                     # read the box back now
        if reason == "done":
            self._emit("info", f"frequency sweep done at {self._freq / 1e6:.6f} MHz")
        elif reason.startswith("error"):
            self._emit("error", f"frequency sweep ended: {reason}")

    # the stream verbs: the sweep's record of every frequency it sent
    def stream_start(self) -> int:
        return self._ramp.stream_start()

    def stream_read(self) -> dict:
        return self._ramp.stream_read()

    def stream_stop(self) -> dict:
        return self._ramp.stream_stop()

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot. Never touches the hardware (see module doc).

        The sweep's fields are laid over it LIVE (in memory, no hardware): the
        snapshot is only rebuilt at poll_hz, and a fly scan waiting for the
        end of a sweep should not wait a poll period for nothing."""
        r = self._ramp.status()
        st = self._status
        live = {"ramping": r["ramping"], "ramp_id": r["ramp_id"],
                "ramp_target_Hz": r["ramp_target"] or 0.0,
                "ramp_rate_Hz_per_s": r["ramp_rate"] or 0.0}
        if all(getattr(st, k) == v for k, v in live.items()):
            return st                        # nothing to lay over: the snapshot itself
        return replace(st, **live)           # a COPY: the snapshot is never edited

    def _offline_snapshot(self) -> Status:
        lim = self.limits()
        return Status(rf_on=False, frequency_Hz=self._freq, power_dBm=self._power,
                      phase_deg=self._phase, vernier=self._vernier,
                      reference=self._reference,
                      connected=False, has_phase=self._has_phase,
                      has_vernier=self._has_vernier, fine_power=self.fine_power(),
                      attenuator_dBm=self._att,
                      power_calibrated=self._cal is not None and self.fine_power(),
                      power_calibration=self._cal.describe() if self._cal else "",
                      freq_min_Hz=lim["freq_min_Hz"], freq_max_Hz=lim["freq_max_Hz"],
                      power_min_dBm=lim["power_min_dBm"],
                      power_max_dBm=lim["power_max_dBm"])

    def _poll_once(self) -> None:
        """Read the unit back and publish a NEW snapshot (one assignment)."""
        if not self._connected:
            return
        prev = self._status
        lim = self.limits()
        try:
            with self._io:
                b = self.backend
                freq = float(b.read_frequency())
                att = float(b.read_power())
                vern = int(b.read_vernier()) if self._has_vernier else 0
                fine = self.fine_power()
                # with fine power the published level is attenuator + vernier
                # (what is delivered) -- a scan's echo waits for THAT; a big
                # manual trim outside the model is left out (warned at start)
                power = (self._delivered(att, vern, freq)
                         if fine and abs(vern) <= vernier_cal.MAX_FILL_COUNTS else att)
                st = Status(
                    rf_on=bool(b.read_output()),
                    frequency_Hz=freq,
                    power_dBm=power,
                    phase_deg=float(b.read_phase()) if self._has_phase else 0.0,
                    vernier=vern,
                    fine_power=fine, attenuator_dBm=att,
                    power_calibrated=fine and self._cal is not None,
                    power_calibration=self._cal.describe() if self._cal else "",
                    reference=b.read_reference(),
                    ext_ref_detected=bool(b.external_ref_detected()),
                    usb_volts=float(b.usb_volts()),
                    connected=True, has_phase=self._has_phase,
                    has_vernier=self._has_vernier, idn=self._idn,
                    freq_min_Hz=lim["freq_min_Hz"], freq_max_Hz=lim["freq_max_Hz"],
                    power_min_dBm=lim["power_min_dBm"],
                    power_max_dBm=lim["power_max_dBm"],
                    polls=prev.polls + 1)
                errs = []
                now = time.monotonic()
                if now - self._last_err_check >= 1.0:      # the queue, once a second
                    self._last_err_check = now
                    errs = b.errors()
        except Exception as exc:
            # Keep the last good values, but say loudly that they are stale.
            msg = f"{type(exc).__name__}: {exc}"
            if msg != prev.hw_error:
                self._emit("error", f"read-back failed: {msg}")
            st = Status(**{**prev.__dict__, "hw_error": msg, "polls": prev.polls + 1})
            errs = []
            self._try_reopen()
        else:
            if prev.hw_error:
                self._emit("info", "hardware link recovered")
        self._status = st
        for e in errs:
            self._emit("warn", f"instrument error: {e}")

    #: while reads fail, re-open the link at most this often (s)
    REOPEN_S = 3.0

    def _try_reopen(self) -> None:
        """A dead link heals itself once the unit is back.

        Found on the office PC (2026-10-01): Windows Update replaced the FTDI
        driver of the generator's USB adapter while the service ran; the COM
        port vanished and came back, and every write then failed with "Access
        is denied" -- for good, because the open port handle belonged to the
        old device. Now the brain re-opens the link (the backend's ``reopen``:
        drop the dead handle without sending, open again, read *IDN? -- it
        changes nothing on the unit) at most every REOPEN_S while reads fail.
        A failed attempt is quiet; the read-back error already says it all.
        """
        reopen = getattr(self.backend, "reopen", None)
        now = time.monotonic()
        if reopen is None or now - self._last_reopen < self.REOPEN_S:
            return
        self._last_reopen = now
        try:
            with self._io:
                reopen()
            self._emit("info", "link to the unit re-opened")
        except Exception:
            pass

    def _poller(self) -> None:
        """Read back at `poll_hz`, or at once after a command (the poke).

        Scheduled on deadlines with short time.sleep() slices rather than
        Event.wait(timeout): on Windows a timed wait sleeps at least one
        15.6 ms tick (gotcha #34)."""
        period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_t or self._poke.is_set():
                self._poke.clear()
                self._poll_once()
                next_t = time.monotonic() + period
            time.sleep(0.005)

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def _cfg_snapshot(self) -> dict:
        """The config values whose CHANGE means "send this to the unit"."""
        hw = self.cfg.hardware
        return {"signal": asdict(self.cfg.signal),
                "limits": asdict(self.cfg.limits),
                "power_calibration": str(getattr(hw, "power_calibration", "")),
                "mute_buzzer": bool(hw.mute_buzzer),
                "display_off": bool(hw.display_off)}

    def apply_config(self) -> None:
        """Act on what CHANGED in the config. Called after set_config (or the
        Settings dialog) edited self.cfg in place.

        Only a change is an instruction. The Settings dialog and set_config
        send the WHOLE config, so treating every field as "push this" would
        overwrite the adopted instrument state with a stale preset every time
        someone changed the theme.
          * a changed `signal` preset field -> set that value (clamped),
          * changed `limits` -> re-clamp the desired signal (and the vernier)
            and push any value that moved (a narrower ceiling must bite at once),
          * changed mute_buzzer / display_off -> *BUZZER / *DISPLAY.
        """
        now, prev = self._cfg_snapshot(), self._seen
        self._seen = now
        sig, old = now["signal"], prev["signal"]
        lim = self.limits()
        if sig["frequency_Hz"] != old["frequency_Hz"]:
            self.set_frequency(sig["frequency_Hz"])
        elif now["limits"] != prev["limits"]:
            v = _clamp(self._freq, lim["freq_min_Hz"], lim["freq_max_Hz"])[0]
            if v != self._freq:
                self.set_frequency(v)
        if sig["power_dBm"] != old["power_dBm"]:
            self.set_power(sig["power_dBm"])
        elif now["limits"] != prev["limits"]:
            v = _clamp(self._power, lim["power_min_dBm"], lim["power_max_dBm"])[0]
            if v != self._power:
                self.set_power(v)
        if self._has_phase or not self._connected:
            if sig["phase_deg"] != old["phase_deg"]:
                self.set_phase(sig["phase_deg"])
            elif now["limits"] != prev["limits"]:
                v = _clamp(self._phase, lim["phase_min_deg"], lim["phase_max_deg"])[0]
                if v != self._phase:
                    self.set_phase(v)
        # the vernier has no preset; only a narrower limit can move it
        if (self._has_vernier and now["limits"] != prev["limits"]):
            v = int(_clamp(self._vernier, lim["vernier_min"], lim["vernier_max"])[0])
            if v != self._vernier:
                self.set_vernier(v)
        if sig["reference"] != old["reference"] and sig["reference"] in REFERENCES:
            self.set_reference(sig["reference"])
        if now["power_calibration"] != prev["power_calibration"]:
            # a new calibration file: load it and deliver the SAME asked power
            # with it (a re-split), so the change takes effect at once
            self.load_power_calibration()
            if self.fine_power() and sig["power_dBm"] == old["power_dBm"]:
                self.set_power(self._power)
        if now["mute_buzzer"] != prev["mute_buzzer"]:
            self._push(self.backend.set_buzzer, not now["mute_buzzer"])
            self._emit("info", f"buzzer {'muted' if now['mute_buzzer'] else 'on'}")
        if now["display_off"] != prev["display_off"]:
            self._push(self.backend.set_display, not now["display_off"])
            self._emit("info", f"display {'off' if now['display_off'] else 'on'}")
        if not self._connected:
            self._status = self._offline_snapshot()

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
