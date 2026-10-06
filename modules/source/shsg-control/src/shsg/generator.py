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

Same outer shape as the suite's other brains -- start(), shutdown(), status(),
get_config()/apply_config(), and an `_on_event` hook the service replaces to
forward events over the wire.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .backends.base import TGSource
from .config import Config


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
        if keep_outputs and self._connected:
            self._emit("info", "TG left as it is (restart)")
        try:
            if (self._connected and self.cfg.hardware.off_on_shutdown
                    and not keep_outputs):
                st = self.backend.read_state()
                # "unknown" counts as possibly on: off is the safe direction
                if (st.get("rf_on") or st.get("tg_unknown")) and st.get("reachable"):
                    try:
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
            if not self._connected:
                raise Refused("not connected (the service has not started the TG backend)")
            st = self.backend.read_state()
            if st.get("hw_error"):
                raise Refused(f"refused: {st['hw_error']}")
            if st.get("tg_busy"):
                raise Refused("refused: a network-analyser sweep holds the tracking "
                              "generator (tg_busy); try again when the sweep ends")
            self.backend.set_cw(**kw)
        except Exception as exc:
            self._emit("error", str(exc))
            raise

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. While connected: the backend's APPLIED state (for the real
        TG, the owner's echo), in one piece. Otherwise our placeholders."""
        if not self._connected:
            return Status(self._rf_on, self._power, self._freq, False, "",
                          parked=not self._rf_on)
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
