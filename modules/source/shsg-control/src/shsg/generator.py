"""The Generator: the small "brain" between the wire and the backend.

A CW source needs no control loop, so this is simple. Its whole job is:

  * CLAMP every request to the configured safety limits (and announce a clamp
    as a warning event, so nothing silently drives the sample too hard),
  * REFUSE a request while the tracking generator is not ours to command --
    a scalar-network-analyser sweep holds it (`tg_busy`), no TG is attached,
    or the signalhound service is not reachable -- with a message that says
    which, instead of queueing it for later (Lukas, 2026-09-28: refusing is
    simple and honest; a queued command would fire at a moment nobody chose),
  * push the accepted value to the backend (simulator, or the signalhound
    service for the real TG),
  * report a status() snapshot. While connected that snapshot is ALWAYS the
    backend's applied state, never our wish: for the real TG it is the
    signalhound service's echo, so a scan waiting for the echo cannot be fooled
    by a value that was only requested (the spirit of gotcha #40).

ADOPT ON START (Lukas's rule, 2026-09-27): start() only READS the TG -- output
on/off, frequency, level -- and takes those as the current signal. Nothing is
written at start, so a TG that already feeds an experiment keeps doing so.

"OFF" IS A PARK (found on the lab PC, 2026-09-28): the TG44A has no off; it
keeps emitting its last frequency and level even after every program exits.
So set_rf(False) PARKS it -- the owner moves it to a park frequency (default
10 kHz) at the minimum level (-30 dBm) -- and status says `parked` with where,
so nobody believes the TG is silent.

STOP: on a CLEAN shutdown the TG is parked when `hardware.off_on_shutdown` is
set (the default): this service is the one that switches the CW on. A killed
service sends nothing, and the TG stays as it is (the signalhound service
parks it when IT stops).

SWEEPS (2026-10-10, for fly scans): frequency and level can be walked
continuously at a set pace (ramp_frequency / ramp_power, see "the SWEEPS"
below). The TG has no phase control, so there is no phase sweep.

Same outer shape as the suite's other brains -- start(), shutdown(), status(),
get_config()/apply_config(), and an `_on_event` hook the service replaces to
forward events over the wire.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field

from .backends.base import TGSource
from .config import Config
from .softramp import SoftRamp

#: The knobs a sweep can walk, and how each one is named:
#: knob -> (brain attribute, wire unit, rate unit on the wire, limit field
#: prefix in config.Limits, argument of the backend's set_cw).
#: The pace limits are config.Limits ramp_rate_min/max_<rate unit>.
SWEEP_KNOBS = {
    "frequency": ("_freq", "Hz", "Hz_per_s", "freq", "freq_hz"),
    "power": ("_power", "dBm", "dB_per_s", "power", "level_dbm"),
}

#: How long after this service last sent a knob the owner's echo may still be
#: the OLD value (the signalhound service publishes ~10 Hz, plus the trip).
#: A sweep that starts later than this starts from the echo (see ramp()).
ECHO_SETTLE_S = 1.0


class Refused(RuntimeError):
    """A command the TG cannot take right now (busy / absent / unreachable)."""


@dataclass
class Status:
    """One snapshot of the generator, for status() and the wire."""

    rf_on: bool
    power_dBm: float
    frequency_Hz: float
    connected: bool
    idn: str = ""
    parked: bool = False        # "off": the TG44A cannot be silenced, so it is PARKED
    park_Hz: float | None = None  # ...at this frequency
    park_dBm: float | None = None  # ...and this (minimum) level
    tg_busy: bool = False       # a network-analyser sweep holds the TG
    tg_unknown: bool = False    # the TG's state could not be read (may be emitting)
    hw_error: str = ""          # "" = the values above are the TG's own
    # The SWEEPS, flat wire keys (see Generator.sweep_status): `ramping` = any
    # knob sweeping; per knob `<knob>_ramping`, `<knob>_ramp_id`, and the
    # target and pace in wire units (e.g. frequency_ramp_target_Hz).
    sweep: dict = field(default_factory=dict)

    @property
    def tg_ready(self) -> bool:
        """The values above are the TG's AND it is delivering them: connected,
        no hardware error, not held by a sweep, state known. What a scan's
        settle rule waits for (together with the echo of its value)."""
        return (self.connected and not self.hw_error and not self.tg_busy
                and not self.tg_unknown)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Generator:
    def __init__(self, backend: TGSource, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        # Placeholders until start() adopts the TG's own values; shown (with
        # hw_error set) only while the backend has never reported anything.
        s = self.cfg.signal
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._rf_on = bool(s.rf_on)
        self._connected = False
        # The [signal] group as last seen, so apply_config() can tell which
        # default the user actually CHANGED (only those get applied).
        self._signal_seen = asdict(s)
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None
        # ONE lock around every COMMAND to the backend. Two threads may
        # command the TG: the service's commander (setters) and, while a sweep
        # runs, the sweep's own thread. The lock keeps one command (its
        # readiness check AND its tg_cw) in one piece. status() does NOT take
        # it: read_state() never talks to the hardware (a cache of the owner's
        # status stream, or the simulator's memory), and the publisher must
        # never wait behind a command stuck in a 1.5 s owner timeout.
        # RLock: a setter that calls another setter must not deadlock itself.
        self._io = threading.RLock()
        # THE SWEEPS (fly scans, 2026-10-10): one software ramp per knob
        # (softramp.py, copied byte for byte from suite-common). The TG has
        # no sweep a fly scan could follow sample by sample (its TG sweep
        # belongs to the network analyser, shsna), so THIS service walks the
        # knob: one tg_cw per step, every value sent recorded with its time.
        self._sweeps = {
            knob: SoftRamp(self._sweep_setter(knob),
                           (lambda a=spec[0]: getattr(self, a)),
                           limits=(lambda k=knob: self._knob_limits(k)),
                           dt_s=float(self.cfg.hardware.ramp_dt_s),
                           on_done=(lambda rid, why, k=knob: self._sweep_done(k, why)),
                           channel=knob, name=f"shsg-{knob}-sweep")
            for knob, spec in SWEEP_KNOBS.items()}
        self._stream_id = 0
        # when this service last handed each knob to the backend (monotonic)
        self._sent_at = {k: -math.inf for k in SWEEP_KNOBS}
        # a parked sweep stores its values without sending them (see ramp());
        # True = the stored value has not reached the owner yet
        self._unsent = {k: False for k in SWEEP_KNOBS}
        # steps the owner accepted but applied later (its reply `deferred`)
        self._deferred = {k: 0 for k in SWEEP_KNOBS}

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend and ADOPT what the TG is doing right now (reads only)."""
        self.backend.open()
        self._connected = True
        st = self.backend.read_state()
        self._signal_seen = asdict(self.cfg.signal)
        if st.get("hw_error"):
            self._emit("error", f"{st['hw_error']} -- commands are refused until "
                                f"this clears (status keeps showing it)")
            return
        if st.get("tg_unknown"):
            # The owner cannot read the TG (found on the real kit: it can be
            # left emitting by another program). Its numbers mean nothing, so
            # we adopt NOTHING and ask for an explicit setting instead.
            self._emit("warn", "TG state unknown -- it may be emitting. Nothing "
                               "adopted; set frequency, level and CW on/off explicitly")
            return
        self._adopt(st)
        self._emit("info", f"connected: {self.backend.idn() or 'USB-TG44A'}")
        self._emit("info", f"adopted from the TG: {'CW ON' if self._rf_on else 'parked (RF off)'}, CW setting "
                           f"{self._freq:g} Hz, {self._power:g} dBm (nothing written)")
        lim = self.cfg.limits
        for name, value, lo, hi, unit in (
                ("frequency", self._freq, lim.freq_min_Hz, lim.freq_max_Hz, "Hz"),
                ("level", self._power, lim.power_min_dBm, lim.power_max_dBm, "dBm")):
            if not lo <= value <= hi:
                self._emit("warn", f"TG {name} {value:g} {unit} is outside the limits "
                                   f"{lo:g}..{hi:g}; left as is (not clamped)")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Clean stop: CW off (if configured and on), disconnect. Safe to repeat.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect the same, but leave the TG as it is whatever
        off_on_shutdown says -- the next start adopts it."""
        # no sweep step may follow the park below
        for ramp in self._sweeps.values():
            ramp.stop()
        if keep_outputs and self._connected:
            self._emit("info", "TG left as it is (restart)")
        try:
            if (self._connected and self.cfg.hardware.off_on_shutdown
                    and not keep_outputs):
                st = self.backend.read_state()
                # "unknown" counts as possibly on: off is the safe direction
                if (st.get("rf_on") or st.get("tg_unknown")) and st.get("reachable"):
                    try:
                        with self._io:
                            self.backend.set_cw(on=False)
                        self._rf_on = False
                        self._emit("info", "CW off -- TG parked (clean stop)")
                    except Exception as exc:
                        # e.g. an SNA sweep holds the TG right now: say so; the
                        # owner decides what the TG does after its sweep.
                        self._emit("warn", f"could not switch the CW off on stop: {exc}")
        finally:
            try:
                self.backend.close()
            finally:
                if self._connected:
                    self._emit("info", "disconnected")
                self._connected = False

    # ---- commands (each clamps, checks, then pushes) ----------------------

    def set_rf(self, on: bool) -> None:
        # Switching the output is a new instruction: a running sweep ends
        # where it is (stopped BEFORE the lock, which a sweep step may be
        # waiting for). A sweep itself never calls this -- it never switches
        # the CW on or off, and never unparks the TG.
        if self.ramp_stop(quiet=True):
            self._emit("info", "sweep stopped by a CW on/off")
        self._command(on=bool(on))
        self._rf_on = bool(on)
        self._emit("info", "CW output ON requested" if on else
                   "CW off requested -- the TG44A cannot be silenced: it is PARKED "
                   "(park frequency, minimum level)")

    def rf_off(self) -> None:
        """RF off (= PARK on the TG44A). Same as set_rf(False); a name of its
        own because over the wire it is the SAFETY verb a viewer may always
        send (net/service.py, control)."""
        self.set_rf(False)

    def set_power(self, dBm: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock in _command, which a sweep step may hold)
        if self._sweeps["power"].stop():
            self._emit("info", "level sweep stopped by a level set")
        lim = self.cfg.limits
        value, clamped = _clamp(float(dBm), lim.power_min_dBm, lim.power_max_dBm)
        self._command(level_dbm=value)
        self._power = value
        if clamped:
            self._emit("warn", f"level clamped to {value:g} dBm "
                               f"(limit {lim.power_min_dBm:g}..{lim.power_max_dBm:g})")
        else:
            self._emit("info", f"level = {value:g} dBm requested")

    def set_frequency(self, hz: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock in _command, which a sweep step may hold)
        if self._sweeps["frequency"].stop():
            self._emit("info", "frequency sweep stopped by a frequency set")
        lim = self.cfg.limits
        value, clamped = _clamp(float(hz), lim.freq_min_Hz, lim.freq_max_Hz)
        self._command(freq_hz=value)
        self._freq = value
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz "
                               f"(limit {lim.freq_min_Hz:g}..{lim.freq_max_Hz:g})")
        else:
            self._emit("info", f"frequency = {value:g} Hz requested")

    def _command(self, **kw) -> None:
        """Refuse early with a clear reason, else hand the command to the backend.

        The backend (the signalhound service) refuses too -- it is the
        authority, the TG can become busy between our check and its arrival.
        Our own check only makes the common case say WHY in plain words.
        Every refusal is also an error EVENT, so a GUI log shows it even when
        the caller was a script.
        """
        try:
            with self._io:
                if not self._connected:
                    raise Refused("not connected (the service has not started the TG backend)")
                st = self.backend.read_state()
                if st.get("hw_error"):
                    raise Refused(f"refused: {st['hw_error']}")
                if st.get("tg_busy"):
                    raise Refused("refused: a network-analyser sweep holds the tracking "
                                  "generator (tg_busy); try again when the sweep ends")
                self.backend.set_cw(**kw)
                for knob, spec in SWEEP_KNOBS.items():
                    if kw.get(spec[4]) is not None:
                        self._sent_at[knob] = time.monotonic()
                        self._unsent[knob] = False
        except Exception as exc:
            self._emit("error", str(exc))
            raise

    # ---- the SWEEPS (fly scans) ------------------------------------------
    #
    # Why: a fly scan (scan-core, `type: fly` axis) records the detectors
    # while a knob moves CONTINUOUSLY and sorts every sample into the pixel of
    # the value the knob had at that moment. The TG jumps to the value it is
    # told, so this service walks it: ramp_frequency / ramp_power start a walk
    # at a set pace, ramp_stop ends it where it is, and an ordinary set of
    # the same knob (or a CW on/off, or shutdown) takes the knob over.
    #
    # EVERY STEP GOES THROUGH THE OWNER (the signalhound service): one tg_cw
    # with only that knob's value, so a step can never switch the output nor
    # touch the other knob. A step the owner refuses or does not answer
    # (busy with a network-analyser sweep, no TG, timeout, owner gone) RAISES;
    # softramp then ends the walk and _sweep_done reports it as an error event
    # -- a sweep never hangs on a dead owner (RemoteTG fails fast, or times
    # out after hardware.owner_timeout_ms).
    #
    # What a fly scan bins by is the COMMANDED value (describe: readback
    # measured false), decided 2026-10-10. The only read-back that exists is
    # the owner's status echo (tg_cw_freq_hz / tg_cw_level_dbm), and that is
    # the SETTING the owner stored after saSetTg returned, not a measurement
    # of the output -- it would only echo the number just sent, ~10 times a
    # second, i.e. no faster than the steps themselves. saSetTg moves the
    # tone within its ~0.03 s call (measured 2026-09-28), inside one 100 ms
    # step, so the command IS the value (# VERIFY on the rig how long the
    # tone takes to settle after saSetTg returns; only "it moved" was
    # measured). One exception, counted and reported:
    # a step the owner DEFERRED (its hardware lock was busy for > 0.2 s)
    # reaches the TG later than the record says.
    #
    # PARKED (CW off) -- decided 2026-10-10: a sweep is ALLOWED, but it only
    # walks the STORED CW setting (the value the TG takes when CW is switched
    # on), exactly as an ordinary set does while parked. It NEVER switches
    # the CW on and never unparks. While parked the steps are not even sent:
    # each one would make the owner re-park the TG (a USB call and a log
    # line, 10 times a second, for nothing that comes out); the value the
    # walk ends at is handed to the owner ONCE when it ends. Refused, with a
    # reason: not connected, hw_error, tg_busy, and tg_unknown (the TG's
    # values are not known, so there is nowhere honest to start from).

    def _knob_limits(self, knob: str) -> tuple[float, float]:
        """The knob's safety envelope from config.Limits, read LIVE (an edited
        limit applies to the next step of a running sweep, too)."""
        _attr, unit, _runit, pre, _kw = SWEEP_KNOBS[knob]
        lim = self.cfg.limits
        return (float(getattr(lim, f"{pre}_min_{unit}")),
                float(getattr(lim, f"{pre}_max_{unit}")))

    def _rate_limits(self, knob: str) -> tuple[float, float]:
        runit = SWEEP_KNOBS[knob][2]
        lim = self.cfg.limits
        return (float(getattr(lim, f"ramp_rate_min_{runit}")),
                float(getattr(lim, f"ramp_rate_max_{runit}")))

    @staticmethod
    def _not_ready(st: dict) -> str:
        """Why the TG cannot take a sweep step right now ("" = it can)."""
        if st.get("hw_error"):
            return f"refused: {st['hw_error']}"
        if st.get("tg_busy"):
            return ("refused: a network-analyser sweep holds the tracking generator "
                    "(tg_busy)")
        if st.get("tg_unknown"):
            return ("refused: the TG state is unknown -- set frequency, level and "
                    "CW on/off explicitly first")
        return ""

    def _sweep_setter(self, knob: str):
        attr, kw = SWEEP_KNOBS[knob][0], SWEEP_KNOBS[knob][4]

        def step(value: float) -> None:
            """One step, on the sweep's own thread. Quiet (no event per step:
            a sweep is ten steps a second). Raises when the TG cannot take it,
            which ends the walk (softramp) with an error event."""
            with self._io:
                if not self._connected:
                    raise Refused("not connected")
                st = self.backend.read_state()          # cheap: no hardware
                why = self._not_ready(st)
                if why:
                    raise Refused(why)
                if st.get("parked"):
                    # store only (see "PARKED" above); handed over at the end
                    setattr(self, attr, float(value))
                    self._unsent[knob] = True
                    return
                reply = self.backend.set_cw(**{kw: float(value)})
                # stored only once the owner ACCEPTED it: a refused step
                # leaves the last value that really went out
                setattr(self, attr, float(value))
                self._unsent[knob] = False
                self._sent_at[knob] = time.monotonic()
                if isinstance(reply, dict) and reply.get("deferred"):
                    self._deferred[knob] += 1
        return step

    def ramp(self, knob: str, to: float, rate: float) -> int:
        """Sweep `knob` to `to` at `rate` (wire units: Hz or dBm, per second);
        returns the sweep's number. The target is clamped to the knob's
        limits and the pace to the configured sweep paces, both with a
        warning -- like every setter here. A sweep of the same knob already
        running is taken over from wherever it got to. Raises ValueError for
        a bad knob / rate / target and Refused when the TG cannot sweep."""
        if knob not in SWEEP_KNOBS:
            raise ValueError(f"cannot sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        attr, unit, runit_w, _pre, _kw = SWEEP_KNOBS[knob]
        runit = runit_w.replace("_per_s", "/s")
        r = abs(float(rate))
        if not r > 0 or not math.isfinite(r):           # also catches NaN
            raise ValueError("rate must be a finite number > 0")
        target = float(to)
        if not math.isfinite(target):
            raise ValueError(f"target must be a finite number, got {to!r}")
        sw = self._sweeps[knob]
        try:
            if not self._connected:
                raise Refused("refused: not connected (the service has not started "
                              "the TG backend)")
            st = self.backend.read_state()
            why = self._not_ready(st)
            if why:
                raise Refused(why)
        except Refused as exc:
            self._emit("error", f"{knob} sweep {exc}")
            raise
        # Where the walk starts: the value this service last sent (or adopted).
        # If we have not sent this knob for a while, someone else may have
        # changed the TG (the analyser's own window, another client), so the
        # owner's echo is the truth then. Not right after our own send: the
        # echo lags ~0.1 s, and a fly scan starts its next row at once.
        echo = st.get("frequency_Hz" if knob == "frequency" else "power_dBm")
        if (not sw.running and echo is not None and math.isfinite(float(echo))
                and time.monotonic() - self._sent_at[knob] > ECHO_SETTLE_S):
            setattr(self, attr, float(echo))
        lo, hi = self._knob_limits(knob)
        rlo, rhi = self._rate_limits(knob)
        r, rclamped = _clamp(r, rlo, rhi)
        value, clamped = _clamp(target, lo, hi)
        sw.dt_s = max(0.001, float(self.cfg.hardware.ramp_dt_s))   # live config
        rid = sw.start(value, r)
        if clamped or rclamped:
            self._emit("warn", f"{knob} sweep clamped to {value:g} {unit} at {r:g} {runit} "
                               f"(limits {lo:g}..{hi:g} {unit}, {rlo:g}..{rhi:g} {runit})")
        self._emit("info", f"{knob} sweep -> {value:g} {unit} at {r:g} {runit}")
        if st.get("parked"):
            self._emit("warn", f"CW is off (TG parked): this {knob} sweep only walks the "
                               f"stored CW setting -- nothing at these values comes out. "
                               f"A sweep never switches the CW on.")
        return rid

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float) -> int:
        return self.ramp("frequency", hz, rate_Hz_per_s)

    def ramp_power(self, dBm: float, rate_dB_per_s: float) -> int:
        return self.ramp("power", dBm, rate_dB_per_s)

    def ramp_stop(self, knob: str | None = None, quiet: bool = False) -> bool:
        """End a sweep where it is -- of one knob, or of every knob (None).
        True if one was running. A stop: allowed for a viewer too."""
        if knob is not None and knob not in SWEEP_KNOBS:
            raise ValueError(f"no sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        was = False
        for k in ([knob] if knob else list(SWEEP_KNOBS)):
            if self._sweeps[k].stop():
                was = True
                if not quiet:
                    self._emit("info", f"{k} sweep stopped at "
                                       f"{getattr(self, SWEEP_KNOBS[k][0]):g} {SWEEP_KNOBS[k][1]}")
        return was

    def _sweep_done(self, knob: str, reason: str) -> None:
        """A walk ended (on the walk's own thread, after its last step)."""
        attr, unit, _r, _p, kw = SWEEP_KNOBS[knob]
        # A parked walk stored its values without sending them: hand the last
        # one to the owner now, so the CW setting is right when CW goes on.
        # (Not after an error: the owner just failed; the next set will do.)
        if self._unsent[knob] and not reason.startswith("error") and self._connected:
            try:
                with self._io:
                    self.backend.set_cw(**{kw: float(getattr(self, attr))})
                    self._unsent[knob] = False
                    self._sent_at[knob] = time.monotonic()
            except Exception as exc:                      # noqa: BLE001
                self._emit("error", f"{knob} sweep: could not store the final "
                                    f"{getattr(self, attr):g} {unit} in the TG: {exc}")
        n, self._deferred[knob] = self._deferred[knob], 0
        if n:
            self._emit("warn", f"{knob} sweep: {n} step(s) were DEFERRED by the signalhound "
                               f"service (its hardware was busy); the TG got those values "
                               f"later than the sweep's record says")
        if reason == "done":
            self._emit("info", f"{knob} sweep done at {getattr(self, attr):g} {unit}")
        elif reason.startswith("error"):
            self._emit("error", f"{knob} sweep ended: {reason}")

    def sweep_status(self) -> dict:
        """The sweeps' live values as flat wire keys (in memory, no hardware).
        `<knob>_ramp_id` is the newest sweep of that knob started; a caller
        whose sweep has number n waits for `<knob>_ramp_id >= n` and
        `<knob>_ramping` false -- numbered so a "not ramping" from before the
        start can never pass for the end (docs/DEVELOPER_NOTES.md gotcha #17)."""
        out = {"ramping": False}
        for knob, (_attr, unit, runit, _pre, _kw) in SWEEP_KNOBS.items():
            r = self._sweeps[knob].status()
            out[f"{knob}_ramping"] = r["ramping"]
            out[f"{knob}_ramp_id"] = r["ramp_id"]
            out[f"{knob}_ramp_target_{unit}"] = r["ramp_target"]
            out[f"{knob}_ramp_rate_{runit}"] = r["ramp_rate"]
            out["ramping"] = out["ramping"] or r["ramping"]
        return out

    # The stream verbs: ONE stream (group "ramp") with one channel per knob --
    # every value each sweep sent, with the time the owner had accepted it.
    # Each knob keeps its own time stamps (`t_ch`, guide 6b "Streams"): the
    # knobs are walked by separate threads. A knob at rest still contributes
    # its value (softramp records the rest value), so a fly row's lead-in has
    # a value to look up.

    def stream_start(self) -> int:
        for ramp in self._sweeps.values():
            ramp.stream_start()
        self._stream_id += 1
        return self._stream_id

    def stream_read(self) -> dict:
        return self._merge({k: r.stream_read() for k, r in self._sweeps.items()})

    def stream_stop(self) -> dict:
        return self._merge({k: r.stream_stop() for k, r in self._sweeps.items()})

    def _merge(self, chunks: dict) -> dict:
        first = next(iter(chunks.values()))
        return {"id": self._stream_id, "t": first["t"],
                "t_ch": {k: c["t"] for k, c in chunks.items()},
                "values": {k: c["values"][k] for k, c in chunks.items()},
                "delay_s": {k: 0.0 for k in chunks},
                "overflow": any(c["overflow"] for c in chunks.values()),
                "now": time.time()}

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. While connected: the backend's APPLIED state (for the real
        TG, the owner's echo), in one piece. Otherwise our placeholders.

        Built fresh on every call (no worker thread keeps a snapshot here), so
        the sweep keys are simply added to the new object (gotcha #1 does not
        bite: nothing else ever writes into a Status)."""
        sweep = self.sweep_status()          # in memory: no hardware
        if not self._connected:
            return Status(self._rf_on, self._power, self._freq, False, "",
                          parked=not self._rf_on, sweep=sweep)
        st = self.backend.read_state()          # never raises (interface rule)
        freq = st.get("frequency_Hz")
        power = st.get("power_dBm")
        return Status(
            rf_on=bool(st.get("rf_on", False)),
            power_dBm=self._power if power is None else float(power),
            frequency_Hz=self._freq if freq is None else float(freq),
            connected=bool(st.get("reachable", False)),
            idn=self.backend.idn(),
            parked=bool(st.get("parked", False)),
            park_Hz=st.get("park_Hz"),
            park_dBm=st.get("park_dBm"),
            tg_busy=bool(st.get("tg_busy", False)),
            tg_unknown=bool(st.get("tg_unknown", False)),
            hw_error=str(st.get("hw_error", "")),
            sweep=sweep,
        )

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config / the Settings dialog edited self.cfg in place.

          * a [signal] default the user CHANGED is applied now (frequency,
            level). `rf_on` is never switched from here: saving Settings must
            never key the output -- that is the RF button's job only.
          * a value that no longer fits (possibly new) limits is re-clamped.
        A refusal (TG busy, owner down) is reported as a warning and does not
        undo the config change itself.
        """
        new = asdict(self.cfg.signal)
        old = self._signal_seen
        self._signal_seen = new
        lim = self.cfg.limits
        cur = self.status()
        for key, current, setter, lo, hi in (
                ("frequency_Hz", cur.frequency_Hz, self.set_frequency,
                 lim.freq_min_Hz, lim.freq_max_Hz),
                ("power_dBm", cur.power_dBm, self.set_power,
                 lim.power_min_dBm, lim.power_max_dBm)):
            try:
                if new.get(key) != old.get(key):
                    setter(float(new[key]))          # the user changed this default
                elif self._connected and not lo <= current <= hi:
                    setter(current)                  # re-clamp to the new limits
            except Exception as exc:
                self._emit("warn", f"settings saved, but {key} not applied: {exc}")

    # ---- internals -------------------------------------------------------

    def _adopt(self, st: dict) -> None:
        if st.get("frequency_Hz") is not None:
            self._freq = float(st["frequency_Hz"])
        if st.get("power_dBm") is not None:
            self._power = float(st["power_dBm"])
        self._rf_on = bool(st.get("rf_on", self._rf_on))

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
