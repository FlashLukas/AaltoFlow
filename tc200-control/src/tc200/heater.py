"""The Heater: the brain between the wire and the backend (simulated or real TC200).

The TC200 runs its own PID loop, so this is a SET-AND-FORGET brain with one
extra job that matters a great deal for a scan -- saying honestly when a
setpoint has been REACHED -- and one that matters for the lab: keeping an
unattended heater safe. Its jobs:

  * hold the desired setpoint, CLAMPED to the safety envelope (a clamp is
    announced as a warn event). The ceiling is the lowest of our configured
    maximum, the box's TMAX minus a margin, and the box's 200 degC; TMAX is
    read from the box, so the ceiling moves when TMAX does;
  * poll the temperature, the setpoint and the status byte, and decide
    `temperature_stable`: output enabled, no sensor alarm, and
    |set - measured| <= tolerance_C held continuously for stable_time_s;
  * switch the output on and off although the box only knows "toggle" (`ens`):
    read the status byte, toggle only if it is not already right, read again
    to confirm -- all under one lock so nothing can toggle in between;
  * refuse to enable while the box's sensor setting does not match the sensor
    that is really wired (a PT100 read as a PT1000 reads ~10x too cold, and
    the controller would heat without limit -- the manual warns it cannot
    detect this itself).

What it deliberately does NOT do at start: change anything. It ADOPTS the
box's setpoint, output state, sensor, gains, PMAX and TMAX (unless
`hardware.push_on_start` asks it to push the stored settings). At shutdown it
switches the output OFF when `hardware.disable_on_shutdown` (the default,
because an unattended heater is the one thing in the lab that can start a fire).

Threads and locks (the rules the other modules learned the hard way):

  * ONE polling thread reads the hardware. `status()` only copies what that
    thread stored and never touches the serial port, so a slow reply cannot
    stall the status publisher, and a lost link shows as `hw_error` instead of
    a healthy-looking panel full of old numbers.
  * EVERY backend call runs under `_hw` (an RLock): the command thread and the
    polling thread would otherwise interleave bytes on one serial line.
  * A setter clears the stable flag and bumps a command GENERATION in the SAME
    critical section in which it stores the new setpoint (gotcha #1 and the
    adopt-then-flag rule, docs/DEVELOPER_NOTES.md section 8), so no status
    frame shows the new setpoint next to the old point's "reached", and a poll
    that started before the command cannot count its readings towards it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, asdict, fields

from .backends.base import HeaterBackend
from .config import (D_GAIN_RANGE, I_GAIN_RANGE, P_GAIN_RANGE, PMAX_MIN_W, SENSORS,
                     TMAX_MAX_C, TMAX_MIN_C, TSET_MAX_C, Config, Device)

_NAN = float("nan")

#: the TC200 stores the setpoint with one decimal, so a box setpoint that
#: differs from ours by less than this is OUR setpoint, rounded -- not a
#: change made on the front panel
_SETPOINT_RESOLUTION = 0.051


@dataclass
class Status:
    """One snapshot of the heater, for status() and the wire."""

    connected: bool
    simulated: bool = True
    idn: str = ""
    hw_error: str = ""
    # temperature
    setpoint_C: float = _NAN
    temperature_C: float = _NAN
    temperature_error_C: float = _NAN
    temperature_stable: bool = False
    temperature_min_C: float = _NAN      # the LIVE setpoint envelope
    temperature_max_C: float = _NAN
    # output + alarms
    enabled: bool = False
    mode: str = ""                       # "normal" | "cycle"
    sensor_alarm: bool = False
    tmax_alarm: bool = False
    # stored settings, as the box reports them
    sensor: str = ""
    sensor_ok: bool = False              # box setting == the sensor really wired
    p_gain: int = 0
    i_gain: int = 0
    d_gain: int = 0
    pmax_W: float = _NAN
    tmax_C: float = _NAN
    # housekeeping
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


class _Band:
    """"Inside the band" held continuously for a time. `since` is the clock time
    at which the condition last became true; reset the moment it fails or a new
    setpoint arrives."""

    def __init__(self):
        self.since = None
        self.stable = False

    def reset(self):
        self.since = None
        self.stable = False

    def update(self, ok: bool, now: float, hold_s: float) -> None:
        if not ok:
            self.reset()
            return
        if self.since is None:
            self.since = now
        self.stable = (now - self.since) >= hold_s


class Heater:
    def __init__(self, backend: HeaterBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards setpoint, readings, flags
        self._connected = False
        self._idn = ""
        self._hw_error = ""
        # what we ask for (under _lock)
        self._sp = _NAN
        self._gen = 0                       # bumped by every setpoint command
        # what the poll thread last read (under _lock)
        self._temp = _NAN
        self._enabled = False
        self._cycle = False
        self._sensor_alarm = False
        self._tmax_alarm = False
        # the box's stored settings as last read (under _lock). cfg.device is
        # what we WANT; this is what the box HAS. apply_config pushes the
        # difference, so a set_config that did not touch them commands nothing.
        self._dev = Device()
        self._readings = 0
        self._poll_ms = _NAN
        self._last_settings_read = -1e9
        self._band = _Band()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Connect, ADOPT the box's state (or push the stored settings when
        hardware.push_on_start), and start polling. Never touches the output
        or the setpoint. `poll=False` is for tests that step `poll_once()`."""
        hw = self.cfg.hardware
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            st = self.backend.read_status()
            sp = self.backend.read_setpoint()
            with self._lock:
                self._enabled = st.enabled
                self._sp = float(sp)
                self._connected = True
            if hw.push_on_start:
                self._push_on_start(st.enabled)
            self._read_settings(adopt_into_cfg=True)
        with self._lock:
            dev = self._dev
            enabled = self._enabled
        self._emit("info", f"connected: {self._idn}; adopted {sp:.1f} C, output "
                           f"{'ENABLED' if enabled else 'disabled'}, sensor {dev.sensor}, "
                           f"PID {dev.p_gain}/{dev.i_gain}/{dev.d_gain}, PMAX {dev.pmax_W:g} W, "
                           f"TMAX {dev.tmax_C:g} C (nothing commanded)")
        self._check_sensor(announce=True)
        eff = self.temperature_max()
        if math.isfinite(sp) and sp > eff + 1e-9:
            self._emit("warn", f"the box's setpoint {sp:.1f} C is above our limit "
                               f"{eff:g} C (left as it is)")
        self.poll_once()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="tc200-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling, switch the output off when hardware.disable_on_shutdown,
        and disconnect. Safe to call more than once."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        switched_off = False
        try:
            with self._hw:
                if self._connected and self.cfg.hardware.disable_on_shutdown:
                    try:
                        switched_off = self._set_output(False)
                    except Exception as exc:     # still close the port
                        self._emit("error", f"could not switch the heater off: {exc}")
                self.backend.close()
        finally:
            with self._lock:
                was = self._connected
                self._connected = False
            if was:
                self._emit("info", "disconnected" + (
                    " (heater switched OFF)" if switched_off else
                    " (heater left as it is)" if not self.cfg.hardware.disable_on_shutdown
                    else ""))

    # ---- commands ---------------------------------------------------------------

    def temperature_max(self) -> float:
        """The LIVE setpoint ceiling: min(our limit, TMAX - margin, 200 degC)."""
        lim = self.cfg.limits
        with self._lock:
            tmax = self._dev.tmax_C
        ceiling = min(float(lim.temperature_max_C), TSET_MAX_C)
        if math.isfinite(tmax):
            ceiling = min(ceiling, tmax - float(lim.tmax_margin_C))
        return max(ceiling, float(lim.temperature_min_C))

    def set_temperature(self, temperature_C: float) -> None:
        """New setpoint. Fire-and-forget: the box heats on its own; watch
        `temperature_stable`. Does NOT switch the output on."""
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(temperature_C, "temperature_C"),
                                float(lim.temperature_min_C), self.temperature_max())
        with self._hw:
            self._require_connected()
            with self._lock:
                cycle, enabled = self._cycle, self._enabled
            if cycle:
                raise RuntimeError("the TC200 is in CYCLE mode (its own temperature "
                                   "program); switch it to NORMAL on the front panel")
            self.backend.set_setpoint(value)
            with self._lock:                # setpoint + flag reset: ONE section
                self._sp = value
                self._gen += 1
                self._band.reset()
        if clamped:
            self._emit("warn", f"setpoint clamped to {value:g} C (limits "
                               f"{lim.temperature_min_C:g}..{self.temperature_max():g} C)")
        else:
            self._emit("info", f"setpoint -> {value:g} C")
        if not enabled:
            self._emit("warn", "the heater output is OFF: the setpoint is stored, but "
                               "nothing heats until it is enabled")

    def set_enabled(self, enabled: bool) -> None:
        """Switch the heater output on or off (read, toggle if needed, confirm)."""
        on = bool(enabled)
        with self._hw:
            self._require_connected()
            if on:
                self._check_can_enable()
            changed = self._set_output(on)
        if changed:
            self._emit("info", "heater output " + ("ENABLED" if on else "disabled"))

    def set_p_gain(self, p: int) -> None:
        self._set_gain("p_gain", p, P_GAIN_RANGE, self.backend.set_p_gain)

    def set_i_gain(self, i: int) -> None:
        self._set_gain("i_gain", i, I_GAIN_RANGE, self.backend.set_i_gain)

    def set_d_gain(self, d: int) -> None:
        self._set_gain("d_gain", d, D_GAIN_RANGE, self.backend.set_d_gain)

    def set_pid(self, p: int, i: int, d: int) -> None:
        self.set_p_gain(p)
        self.set_i_gain(i)
        self.set_d_gain(d)

    def set_pmax(self, watts: float) -> None:
        """Output power ceiling, clamped to 0.1 W .. limits.pmax_max_W."""
        hi = max(PMAX_MIN_W, float(self.cfg.limits.pmax_max_W))
        v, clamped = _clamp(round(_finite(watts, "pmax_W"), 1), PMAX_MIN_W, hi)
        with self._hw:
            self._require_connected()
            self.backend.set_pmax(v)
            with self._lock:
                self._dev.pmax_W = v
            self.cfg.device.pmax_W = v
        self._emit("warn" if clamped else "info",
                   f"PMAX {'clamped to' if clamped else '='} {v:g} W")

    def set_tmax(self, tmax_C: float) -> None:
        """The box's over-temperature trip. Moves our setpoint ceiling with it;
        the box drags its own setpoint down if TMAX goes below it."""
        v, clamped = _clamp(round(_finite(tmax_C, "tmax_C"), 1), TMAX_MIN_C, TMAX_MAX_C)
        with self._hw:
            self._require_connected()
            self.backend.set_tmax(v)
            sp = self.backend.read_setpoint()
            with self._lock:
                self._dev.tmax_C = v
                if abs(sp - self._sp) > _SETPOINT_RESOLUTION:
                    self._sp = float(sp)
                    self._gen += 1
                    self._band.reset()
                    lowered = True
                else:
                    lowered = False
            self.cfg.device.tmax_C = v
        self._emit("warn" if clamped else "info",
                   f"TMAX {'clamped to' if clamped else '='} {v:g} C "
                   f"(setpoint ceiling now {self.temperature_max():g} C)")
        if lowered:
            self._emit("warn", f"the TC200 lowered its setpoint to {sp:g} C with TMAX")

    def set_sensor(self, sensor: str) -> None:
        """Select the sensor type on the box. Refused while the output is on: a
        wrong type makes the controller misread the temperature."""
        s = str(sensor).strip().lower()
        if s not in SENSORS:
            raise ValueError(f"sensor must be one of {', '.join(SENSORS)}; got {sensor!r}")
        with self._hw:
            self._require_connected()
            if self.backend.read_status().enabled:
                raise RuntimeError("switch the heater off before changing the sensor type")
            self.backend.set_sensor(s)
            with self._lock:
                self._dev.sensor = s
            self.cfg.device.sensor = s
        self._emit("info", f"sensor = {s}")
        self._check_sensor(announce=True)

    # ---- status -------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot of what the poll thread last read. Never touches hardware."""
        lim = self.cfg.limits
        tmax_eff = self.temperature_max()
        expected = self.cfg.hardware.expected_sensor
        with self._lock:
            d = self._dev
            return Status(
                connected=self._connected,
                simulated=bool(getattr(self.backend, "simulated", True)),
                idn=self._idn,
                hw_error=self._hw_error,
                setpoint_C=self._sp,
                temperature_C=self._temp,
                temperature_error_C=self._temp - self._sp,
                temperature_stable=self._band.stable,
                temperature_min_C=float(lim.temperature_min_C),
                temperature_max_C=tmax_eff,
                enabled=self._enabled,
                mode="cycle" if self._cycle else "normal",
                sensor_alarm=self._sensor_alarm,
                tmax_alarm=self._tmax_alarm,
                sensor=d.sensor,
                sensor_ok=(d.sensor == expected),
                p_gain=int(d.p_gain), i_gain=int(d.i_gain), d_gain=int(d.d_gain),
                pmax_W=float(d.pmax_W), tmax_C=float(d.tmax_C),
                readings=self._readings,
                poll_ms=self._poll_ms,
            )

    # ---- settings (Settings dialog / wire use these) ----------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-check the settings after set_config edited self.cfg in place.

        The `device` group is pushed to the box, but only the fields that DIFFER
        from what the box has -- so a coordinator pushing back an unchanged
        config commands nothing (gotcha #5). The setpoint and the output are
        never touched here; a setpoint outside a NEW envelope is announced, not
        re-driven."""
        if self._connected:
            with self._lock:
                have = Device(**asdict(self._dev))
            want = self.cfg.device
            for f in fields(Device):
                a, b = getattr(want, f.name), getattr(have, f.name)
                if a == b or (isinstance(a, float) and isinstance(b, float)
                              and abs(a - b) < 1e-9):
                    continue
                try:
                    if f.name == "sensor":
                        self.set_sensor(a)
                    elif f.name == "p_gain":
                        self.set_p_gain(a)
                    elif f.name == "i_gain":
                        self.set_i_gain(a)
                    elif f.name == "d_gain":
                        self.set_d_gain(a)
                    elif f.name == "pmax_W":
                        self.set_pmax(a)
                    elif f.name == "tmax_C":
                        self.set_tmax(a)
                except Exception as exc:
                    # keep cfg honest: it shows what the box really has
                    setattr(want, f.name, b)
                    self._emit("warn", f"{f.name} not changed: {exc}")
        with self._lock:
            sp = self._sp
        hi = self.temperature_max()
        if math.isfinite(sp) and not self.cfg.limits.temperature_min_C <= sp <= hi + 1e-9:
            self._emit("warn", f"setpoint {sp:g} C is outside the new limits "
                               f"{self.cfg.limits.temperature_min_C:g}..{hi:g} C (not changed)")

    # ---- polling -------------------------------------------------------------------

    def _poll_loop(self) -> None:
        # Deadline scheduling (gotcha #34: a timed Event.wait sleeps in 15.6 ms
        # ticks on Windows; harmless at 2 Hz, but the rule is cheap to follow).
        period = max(0.05, float(self.cfg.hardware.poll_s))
        next_t = time.monotonic()
        while not self._stop.is_set():
            self.poll_once()
            period = max(0.05, float(self.cfg.hardware.poll_s))
            next_t += period
            now = time.monotonic()
            if next_t < now:                  # fell behind (slow link): do not burst
                next_t = now
            while not self._stop.is_set() and time.monotonic() < next_t:
                time.sleep(min(0.05, max(0.0, next_t - time.monotonic())))

    def poll_once(self) -> None:
        """Read temperature, status byte and setpoint (and, every
        settings_poll_s, the stored settings) and update the flags."""
        with self._lock:
            gen = self._gen
        t0 = self._clock()
        try:
            with self._hw:
                temp = self.backend.read_temperature()
                st = self.backend.read_status()
                sp = self.backend.read_setpoint()
                if t0 - self._last_settings_read >= float(self.cfg.hardware.settings_poll_s):
                    self._read_settings(adopt_into_cfg=False)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            with self._lock:
                new = msg != self._hw_error
                self._hw_error = msg
                self._band.reset()          # nothing read -> nothing is reached
            if new:
                self._emit("error", f"TC200 read failed: {msg}")
            return
        now = self._clock()
        t = self.cfg.temperature
        events = []
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            was_enabled = self._enabled
            self._temp = float(temp)
            self._enabled = bool(st.enabled)
            self._cycle = bool(st.cycle_mode)
            if st.sensor_alarm and not self._sensor_alarm:
                events.append(("error", "SENSOR ALARM: open or shorted sensor; the TC200 "
                                        "has switched the heater off"))
            if st.tmax_alarm and not self._tmax_alarm:
                events.append(("error", "TMAX ALARM: the temperature reached TMAX and the "
                                        "TC200 opened its output relay"))
            self._sensor_alarm = bool(st.sensor_alarm)
            self._tmax_alarm = bool(st.tmax_alarm)
            if was_enabled and not self._enabled:
                events.append(("warn", "the heater output went OFF (front panel, alarm, "
                                       "or third TMAX trip)"))
            elif self._enabled and not was_enabled:
                events.append(("info", "the heater output went ON (front panel)"))
            self._readings += 1
            self._poll_ms = (now - t0) * 1000.0
            if gen == self._gen:
                # A setpoint changed on the FRONT PANEL (or by TMAX): adopt it,
                # like any new setpoint -- flag cleared, generation bumped.
                if abs(float(sp) - self._sp) > _SETPOINT_RESOLUTION:
                    events.append(("info", f"setpoint changed on the TC200: {self._sp:g} "
                                           f"-> {float(sp):g} C (adopted)"))
                    self._sp = float(sp)
                    self._gen += 1
                    self._band.reset()
                else:
                    # A command that arrived WHILE we were reading makes these
                    # readings older than the setpoint: they must not count.
                    ok = (self._enabled and not self._sensor_alarm
                          and abs(self._temp - self._sp) <= float(t.tolerance_C))
                    self._band.update(ok, now, float(t.stable_time_s))
        for level, msg in events:
            self._emit(level, msg)
        if recovered:
            self._emit("info", "TC200 readings recovered")

    # ---- internals -------------------------------------------------------------------

    def _set_output(self, on: bool) -> bool:
        """Read-then-toggle-then-confirm. Caller holds _hw. Returns True if the
        output was changed. `ens` is a TOGGLE, so blindly sending it would turn
        a heater that is already on OFF (or the other way round)."""
        st = self.backend.read_status()
        if st.enabled == on:
            with self._lock:
                self._enabled = on
            return False
        self.backend.toggle_enable()
        st = self.backend.read_status()
        with self._lock:
            self._enabled = st.enabled
            self._band.reset()
        if st.enabled != on:
            raise RuntimeError(f"the TC200 did not {'enable' if on else 'disable'} "
                               "its output" + (" (sensor alarm)" if st.sensor_alarm else ""))
        return True

    def _check_can_enable(self) -> None:
        """Caller holds _hw. The reasons NOT to heat, checked on the box itself."""
        sensor = self.backend.read_sensor()
        st = self.backend.read_status()
        with self._lock:
            self._dev.sensor = sensor
        expected = self.cfg.hardware.expected_sensor
        if sensor != expected:
            raise RuntimeError(f"refusing to enable: the TC200 is set to sensor {sensor!r} "
                               f"but a {expected!r} is wired (hardware.expected_sensor). "
                               "Fix the sensor setting first.")
        if st.sensor_alarm:
            raise RuntimeError("refusing to enable: sensor alarm (open or shorted sensor)")
        if st.cycle_mode:
            raise RuntimeError("refusing to enable: the TC200 is in CYCLE mode and would "
                               "run its stored temperature program")

    def _set_gain(self, name: str, value, rng, push) -> None:
        v = int(round(_finite(value, name)))
        v, clamped = _clamp(v, rng[0], rng[1])
        v = int(v)
        with self._hw:
            self._require_connected()
            push(v)
            with self._lock:
                setattr(self._dev, name, v)
            setattr(self.cfg.device, name, v)
        self._emit("warn" if clamped else "info",
                   f"{name.replace('_', ' ')} {'clamped to' if clamped else '='} {v}")

    def _read_settings(self, adopt_into_cfg: bool) -> None:
        """Caller holds _hw. Read the box's stored settings. A value that CHANGED
        since the last read was changed on the front panel: adopt it into cfg
        too, so the Settings dialog and set_config show the truth."""
        sensor = self.backend.read_sensor()
        p, i, d = self.backend.read_pid()
        pmax = float(self.backend.read_pmax())
        tmax = float(self.backend.read_tmax())
        new = Device(sensor=sensor, p_gain=int(p), i_gain=int(i), d_gain=int(d),
                     pmax_W=pmax, tmax_C=tmax)
        with self._lock:
            old = self._dev
            self._dev = new
        self._last_settings_read = self._clock()
        changed = [f.name for f in fields(Device)
                   if getattr(old, f.name) != getattr(new, f.name)]
        for f in fields(Device):
            if adopt_into_cfg or f.name in changed:
                setattr(self.cfg.device, f.name, getattr(new, f.name))
        if changed and not adopt_into_cfg:
            self._emit("info", "changed on the TC200 (adopted): " + ", ".join(
                f"{n} = {getattr(new, n)}" for n in changed))
            if "sensor" in changed:
                self._check_sensor(announce=True)

    def _push_on_start(self, enabled: bool) -> None:
        """Caller holds _hw. hardware.push_on_start: push cfg.device to the box.
        The sensor only while the output is off (see set_sensor)."""
        dev = self.cfg.device
        be = self.backend
        if dev.sensor in SENSORS and be.read_sensor() != dev.sensor:
            if enabled:
                self._emit("warn", f"push_on_start: sensor NOT changed to {dev.sensor} "
                                   "while the heater is on")
            else:
                be.set_sensor(dev.sensor)
        be.set_p_gain(int(_clamp(int(dev.p_gain), *P_GAIN_RANGE)[0]))
        be.set_i_gain(int(_clamp(int(dev.i_gain), *I_GAIN_RANGE)[0]))
        be.set_d_gain(int(_clamp(int(dev.d_gain), *D_GAIN_RANGE)[0]))
        be.set_pmax(_clamp(float(dev.pmax_W), PMAX_MIN_W,
                           max(PMAX_MIN_W, float(self.cfg.limits.pmax_max_W)))[0])
        be.set_tmax(_clamp(float(dev.tmax_C), TMAX_MIN_C, TMAX_MAX_C)[0])
        sp = be.read_setpoint()                 # TMAX may have dragged it down
        with self._lock:
            self._sp = float(sp)
        self._emit("info", "pushed the stored settings to the TC200 (push_on_start)")

    def _check_sensor(self, announce: bool) -> None:
        with self._lock:
            sensor = self._dev.sensor
        expected = self.cfg.hardware.expected_sensor
        if sensor != expected and announce:
            self._emit("warn", f"the TC200 is set to sensor {sensor!r}, but a {expected!r} "
                               "is wired: enabling is refused until this is fixed "
                               "(front panel, set_sensor, or hardware.push_on_start)")

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("not connected to the TC200")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
