"""Two more real analysers that speak ONE command language: the LibreVNA
(open-source, 2 ports, 100 kHz - 6 GHz) and the Harogic PNB-60 (handheld,
2 ports, 100 kHz - 6 GHz).

This is the ONLY file that talks to either of them, and it imports pyvisa inside
`open()`, so the package (and every test) works on a PC without it. Install with
`uv sync --extra gui --extra real`.

WHY ONE FILE FOR TWO MAKES. Neither box is programmed directly. Each is driven
by a PC program, and SCPI goes to THAT PROGRAM over a TCP socket -- the same
arrangement as the C1209 and S2VNA (cmt.py):

  * LibreVNA -> **LibreVNA-GUI**, SCPI server on port 19542 (Preferences >
    General > SCPI server; can run without a window: `LibreVNA-GUI --no-gui`).
  * PNB-60   -> **Harogic VNStudio**, SCPI server on port 5025 (System > SCPI >
    Enable; "keep VNStudio running while using SCPI remote control").

Harogic modelled VNStudio's SCPI on LibreVNA-GUI's: the same tree
(`VNA:FREQ:START`, `VNA:ACQ:IFBW`, `VNA:STIM:LVL`, `VNA:ACQ:SINGLE TRUE`,
`DEV:INF:LIM:...`) and the same trace format (`VNA:TRAC:DATA? S21` ->
`[f,re,im],[f,re,im],...`). So one class does the work and a small `Dialect`
holds what really differs:

                        LibreVNA-GUI                  Harogic VNStudio
  port                  19542                         5025
  *IDN? starts with     LibreVNA                      Harogic
  no device             DEV:CONN? -> "Not connected"  DEV:CONN? -> SERVER_NOT_READY
  sweep done?           VNA:ACQ:FIN? -> TRUE          VNA:ACQ:RUN? -> FALSE
  sweep-time query      none (rule of thumb)          VNA:ACQ:SWE:TIME?
  which trace is read   our own trace AALTOFLOW_S,    the S-parameter by name
                        its parameter set by SCPI     (S11 | S12 | S21 | S22)

How a measurement goes (same on both):

  * CONNECT = LOOK (Lukas, 2026-09-27). open() sends queries only; the brain
    adopts the sweep the program already holds. Nothing changes until somebody
    asks THIS module for a trace.
  * The FIRST sweep takes over: frequency sweep, linear, averaging 1 (the brain
    averages), and on LibreVNA our trace. Then single sweeps: write only the
    settings that changed (each read back), then `VNA:ACQ:SINGLE TRUE`, which
    (both manuals) puts the program in single-sweep mode AND starts a new sweep.
  * finish_sweep polls the dialect's "done?" query, then reads the trace. The
    reply carries the frequency of EVERY point, so the grid is checked point by
    point against the one the brain asked for -- a trace filed under the wrong
    frequencies is refused instead of stored.
  * close() puts the program back to continuous sweeping, only if we took it
    over.

ERRORS. Neither manual documents an error queue (`SYST:ERR?`). Both document
`*ESR?`, the IEEE 488.2 event register (bits 0x04 query, 0x08 device,
0x10 execution, 0x20 command error; reading it clears it). VNStudio may also
ANSWER a failed command with a text such as GATEWAY_EXECUTION_ERROR. Such an
unasked-for line would sit in the socket and be read as the reply to the NEXT
query -- every number after it one step out of step. So `_esr()` reads lines
until it gets the register's number and reports every text line before it as
an error. It runs after every batch of settings, before a sweep is started.

UNTESTED ON EITHER INSTRUMENT (2026-10-26). The commands follow the LibreVNA
"SCPI Programming Guide" (LibreVNA repository, Documentation/UserManual) and the
"HAROGIC Vector Network Analyzer SCPI Application Guide V1.0" (23/10/2026). Every
line not yet seen working is marked `# VERIFY` with what to check.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

import numpy as np

from .. import model
from ..config import Config, Limits
from ..field import FieldReading
from . import claim as hwclaim      # the one-service-per-analyser lock


@dataclass(frozen=True)
class Dialect:
    """What differs between LibreVNA-GUI and VNStudio (see the module docstring)."""
    key: str                  # hardware.driver
    name: str                 # for messages
    program: str              # the PC program that owns the box
    resource_field: str       # the Hardware attribute holding its address
    idn_vendor: str           # *IDN? starts with this (case-insensitive)
    model_hint: str           # a different model is a warning, not a failure
    not_connected: tuple      # DEV:CONN? replies that mean "no box attached"
    done_query: str           # polled after the trigger ...
    done_reply: str           # ... until it answers this
    sweep_time_query: str     # "" = none: the duration of the last sweep is learned
    cal_query: str            # "" = none; else the active calibration ("NONE" = off)
    own_trace: bool           # True = read our named trace, False = the S-parameter
    sweep_type: bool          # VNA:SWEEPTYPE LIN exists (Harogic) and must be set
    set_mode: bool            # DEV:MODE VNA exists (LibreVNA: VNA / GEN / SA)
    envelope: dict            # data-sheet limits, pulled into cfg.limits (limit_envelope)
    help: str                 # what to check when open() fails


LIBREVNA = Dialect(
    key="librevna", name="LibreVNA", program="LibreVNA-GUI",
    resource_field="librevna_resource", idn_vendor="LIBREVNA", model_hint="LibreVNA",
    not_connected=("NOT CONNECTED",),
    done_query="VNA:ACQ:FIN?", done_reply="TRUE",
    sweep_time_query="", cal_query="VNA:CAL:ACTIVE?",
    own_trace=True, sweep_type=False, set_mode=True,
    # 100 kHz - 6 GHz (the data sheet). The rest is what LibreVNA-GUI 1.6.5
    # reported for the group's unit on 2026-10-26 (DEV:INF:LIM:*): 4501 points,
    # IFBW 6 Hz - 50 kHz, power -40 .. 0 dBm. (It reports MINF 0; the data
    # sheet's 100 kHz is kept.)
    envelope={"freq_min_Hz": 100e3, "freq_max_Hz": 6e9, "points_max": 4501,
              "ifbw_min_Hz": 6.0, "ifbw_max_Hz": 50e3,
              "power_min_dBm": -40.0, "power_max_dBm": 0.0},
    help="LibreVNA-GUI is running with the LibreVNA connected (Device > Connect), its SCPI "
         "server is on (Window > Preferences > General, port 19542)",
)

HAROGIC = Dialect(
    key="harogic", name="Harogic PNB-60", program="VNStudio",
    resource_field="harogic_resource", idn_vendor="HAROGIC", model_hint="NB-60",
    not_connected=("SERVER_NOT_READY",),
    # VERIFY: RUN? is TRUE while the single sweep runs and FALSE once it ended
    # (the guide: "TRUE indicates that a sweep is running").
    done_query="VNA:ACQ:RUN?", done_reply="FALSE",
    sweep_time_query="VNA:ACQ:SWE:TIME?", cal_query="",
    own_trace=False, sweep_type=True, set_mode=False,
    # 100 kHz - 6 GHz (PNB-60 product page); the rest are the replies printed in
    # the SCPI guide's examples (MINIFBW 15, MAXIFBW 75000, MAXP 2048, power
    # -40 .. 5 dBm).  # VERIFY with DEV:INF:LIM:* on the unit
    envelope={"freq_min_Hz": 100e3, "freq_max_Hz": 6e9, "points_max": 2048,
              "ifbw_min_Hz": 15.0, "ifbw_max_Hz": 75e3,
              "power_min_dBm": -40.0, "power_max_dBm": 5.0},
    help="VNStudio is running with the PNB-60 connected and SCPI enabled "
         "(System > SCPI > Enable, port 5025)",
)

DIALECTS = {d.key: d for d in (LIBREVNA, HAROGIC)}

#: the trace this module owns on LibreVNA-GUI (never deletes anyone else's)
TRACE = "AALTOFLOW_S"

_SETTERS = {
    # key: (write template, read-back query). Hz, points, Hz, dBm.
    "start": ("VNA:FREQ:START {v:.12g}", "VNA:FREQ:START?"),
    "stop": ("VNA:FREQ:STOP {v:.12g}", "VNA:FREQ:STOP?"),
    "points": ("VNA:ACQ:POINTS {v:d}", "VNA:ACQ:POINTS?"),
    "ifbw": ("VNA:ACQ:IFBW {v:.12g}", "VNA:ACQ:IFBW?"),
    # the source power of a FREQUENCY sweep (both programs); one level for both
    # ports.  # VERIFY: also the level of port 2 for S12 / S22
    "power": ("VNA:STIM:LVL {v:.4g}", "VNA:STIM:LVL?"),
}

#: *ESR? bits that mean a command failed (IEEE 488.2)
_ESR_ERRORS = {0x04: "query error", 0x08: "device error",
               0x10: "execution error", 0x20: "command error"}

#: limits the programs report (both: DEV:INF:LIM:...) -> the cfg.limits key
_DEVICE_LIMITS = (("DEV:INF:LIM:MINF?", "freq_min_Hz", max),
                  ("DEV:INF:LIM:MAXF?", "freq_max_Hz", min),
                  ("DEV:INF:LIM:MAXP?", "points_max", min),
                  ("DEV:INF:LIM:MINIFBW?", "ifbw_min_Hz", max),
                  ("DEV:INF:LIM:MAXIFBW?", "ifbw_max_Hz", min),
                  ("DEV:INF:LIM:MINPOW?", "power_min_dBm", max),
                  ("DEV:INF:LIM:MAXPOW?", "power_max_dBm", min))

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?nan|[-+]?inf",
                     re.IGNORECASE)


def limit_envelope(cfg: Config, dialect: Dialect) -> None:
    """Make the config's envelope what the analyser can do, so a setpoint it
    cannot reach is clamped (and announced) by the brain instead of being
    silently moved by the program.

    A limit still at the generic default (Limits(): a PNA-shaped 10 MHz -
    20 GHz) is REPLACED by the data sheet -- otherwise the 100 kHz end these
    analysers reach would stay out of bounds. A limit somebody set in vna.ini
    is only ever narrowed, never widened."""
    lim, env, default = cfg.limits, dialect.envelope, Limits()
    for key, value in env.items():
        cur = getattr(lim, key)
        if cur == getattr(default, key):
            new = value
        else:
            pick = max if "_min_" in key else min
            new = pick(cur, value)
        setattr(lim, key, type(cur)(new))
    sw = cfg.sweep
    sw.stop_Hz = min(float(sw.stop_Hz), lim.freq_max_Hz)
    sw.start_Hz = max(min(float(sw.start_Hz), sw.stop_Hz - lim.min_span_Hz), lim.freq_min_Hz)
    sw.points = min(int(sw.points), int(lim.points_max))
    sw.ifbw_Hz = min(max(float(sw.ifbw_Hz), lim.ifbw_min_Hz), lim.ifbw_max_Hz)
    sw.power_dBm = min(max(float(sw.power_dBm), lim.power_min_dBm), lim.power_max_dBm)


def parse_trace(text: str) -> tuple[np.ndarray, np.ndarray]:
    """`[f,re,im],[f,re,im],...` (both programs) -> (frequencies Hz, complex).

    Refuses anything that is not whole triples: a number lost in transfer
    would shift every point after it and still look like data."""
    nums = [float(x) for x in _NUMBER.findall(str(text))]
    if not nums or len(nums) % 3:
        raise RuntimeError(f"trace data is not [f, re, im] triples ({len(nums)} numbers: "
                           f"{str(text)[:80]!r})")
    a = np.asarray(nums).reshape(-1, 3)
    return a[:, 0], a[:, 1] + 1j * a[:, 2]


class SweepServerVna:
    """A VNA behind LibreVNA-GUI or VNStudio (pick with `dialect`)."""

    simulated = False

    # FLY-SCAN TIMING (the brain's stream, analyzer.py). Point i is stamped at
    # t_start + trigger_latency_s + (i + 0.5) * T / n with T = sweep_time_s().
    # VERIFY: the delay between VNA:ACQ:SINGLE TRUE and the first point, and
    # that the points are evenly spaced in time across the sweep.
    trigger_latency_s = 0.0

    def __init__(self, cfg: Config, dialect: Dialect, resource=None,
                 clock=time.monotonic, sleep=time.sleep):
        """`resource` = an already-open VISA resource. Tests pass a FAKE one
        (anything with write / query / read / close); normally it is None and
        `open()` opens the address in `hardware.<dialect.resource_field>`."""
        self.cfg = cfg
        self.d = dialect
        self._res = resource
        self._rm = None
        self._hwlock = None
        self._clock = clock
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # what the instrument is known to hold
        self._readback: dict = {}         # what it reported back for those
        self._sparam = None
        self._pending = False
        self._prepared = False            # set up for single sweeps yet? (_take_over)
        self._freqs = np.zeros(0)         # the grid of the sweep in flight
        self._started_at = math.nan
        self._sweep_time: dict = {}       # (points, ifbw) -> seconds (Harogic's query)
        self._found_mode = ""             # LibreVNA-GUI's mode at connect (VNA / SA / GEN)
        self._cal = None                  # the active calibration, e.g. "SOLT_12"
        self._correction = None
        self._ifbw_asked = math.nan
        self.device_limits: dict = {}
        self.warnings: list[str] = []

    @property
    def resource_name(self) -> str:
        return str(getattr(self.cfg.hardware, self.d.resource_field))

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        """Claim the analyser, connect, and LOOK (queries only).

        A failed open releases everything it took -- above all the claim, or a
        dead attempt would keep the analyser "busy" for every other service."""
        try:
            self._connect()
        except BaseException:
            self._abandon()
            raise

    def _connect(self) -> None:
        hw = self.cfg.hardware
        address = self.resource_name
        if self._res is None:
            try:
                import pyvisa                      # lazy: only the real backend needs it
            except ImportError as exc:
                raise RuntimeError(
                    "pyvisa is not installed. In vna-control run: "
                    "uv sync --extra gui --extra real") from exc
            # "@py" = pyvisa-py, which opens the TCP socket itself: no vendor
            # VISA library needed. `hardware.visa_library` overrides it.
            self._rm = pyvisa.ResourceManager(hw.visa_library or "@py")
            # ONE SERVICE PER ANALYSER: claim the address BEFORE connecting.
            # On 127.0.0.1 the PORT names the program (claim.py), so a
            # LibreVNA-GUI on 19542 and S2VNA on 5025 never collide.
            self._hwlock = hwclaim.claim(address, self._rm)
            self._res = self._rm.open_resource(address)
        else:
            self._hwlock = hwclaim.claim(address)
        r = self._res
        r.timeout = int(float(hw.timeout_s) * 1000)       # pyvisa counts milliseconds
        # A raw socket has no end-of-message marker: every command and every
        # reply ends in a newline (both guides).
        r.read_termination = "\n"
        r.write_termination = "\n"
        self._applied, self._readback, self._sparam = {}, {}, None
        self._prepared = False

        self._idn = self._query("*IDN?")          # "LibreVNA,LibreVNA-GUI,<sn>,<ver>" /
        #                                           "Harogic,PNB-60,<sn>,<ver>"
        if not self._idn.upper().startswith(self.d.idn_vendor):
            self.warnings.append(f"expected {self.d.name} behind {self.d.program}, "
                                 f"*IDN? says {self._idn!r}")
        elif self.d.model_hint.upper() not in self._idn.upper():
            self.warnings.append(f"expected a {self.d.name}, *IDN? says {self._idn!r}")
        # The program answers even with no box attached -- then every sweep
        # would fail later with a puzzling error. Say it here instead.
        conn = self._query("DEV:CONN?")
        if not conn or conn.upper() in self.d.not_connected:
            raise RuntimeError(f"{self.d.program} has no analyser connected "
                               f"(DEV:CONN? -> {conn!r}): connect it in {self.d.program}")
        self._serial = conn
        self._esr()           # reading the register clears it; old bits are not ours

    def read_state(self) -> dict:
        """What the program is doing now, by queries only (see base.read_state).
        A query it does not know becomes a warning and its key is left out."""
        st: dict = {}
        for key, setter, cast in (("start_Hz", "start", float), ("stop_Hz", "stop", float),
                                  ("points", "points", int), ("ifbw_Hz", "ifbw", float),
                                  ("power_dBm", "power", float)):
            try:
                v = cast(float(self._query(_SETTERS[setter][1])))
            except Exception as exc:
                self.warnings.append(f"reading {key}: {exc}")
                continue
            st[key] = v
            self._applied[setter] = v
            self._readback[setter] = v
        st["sparam"] = None
        if self.d.own_trace:
            # our trace from an earlier run tells which S-parameter we measured
            try:
                if TRACE in self._trace_list():
                    p = self._query(f"VNA:TRAC:PARAM? {TRACE}").upper()   # VERIFY
                    st["sparam"] = p if p in model.SPARAMS else None
            except Exception as exc:
                self.warnings.append(f"reading the trace list: {exc}")
        # VNStudio measures every S-parameter it shows; which one we read is our
        # choice at read time, so there is nothing to adopt there.
        for key, q in (("sweep", "VNA:SWEEP?"), ("single", "VNA:ACQ:SINGLE?"),
                       ("instrument_averages", "VNA:ACQ:AVG?")):
            try:
                st[key] = self._query(q)
            except Exception as exc:
                self.warnings.append(f"reading {key}: {exc}")
        if self.d.set_mode:
            # LibreVNA-GUI is also a spectrum analyser and a generator: note the
            # mode it is in, so close() can put it back (it was "SA" on the
            # first real run, 2026-10-26)
            try:
                self._found_mode = self._query("DEV:MODE?").upper()
                st["mode"] = self._found_mode
            except Exception as exc:
                self.warnings.append(f"reading the mode: {exc}")
        if self.d.cal_query:
            st["calibration"] = self._calibration()
            st["correction_on"] = self._correction
        st["serial"] = getattr(self, "_serial", "")
        self._read_device_limits()
        st["device_limits"] = dict(self.device_limits)
        st["continuous"] = False       # driving it means taking over: start hands-off
        self._esr(warn=True)
        return st

    def _read_device_limits(self) -> None:
        """The limits the program reports, compared with the envelope: a limit
        TIGHTER than this module's is a warning (put it in vna.ini [limits])."""
        lim = self.cfg.limits
        for q, key, tighter in _DEVICE_LIMITS:
            try:
                v = float(self._query(q))
            except Exception:
                continue                       # VERIFY: all seven exist on both
            self.device_limits[key] = v
            ours = float(getattr(lim, key))
            if tighter(ours, v) != ours:
                self.warnings.append(f"{self.d.program} reports {key} = {v:g}, tighter than "
                                     f"this module's {ours:g}: set it in vna.ini [limits]")

    def _take_over(self) -> None:
        """Set the program up for single sweeps on demand. Runs at the FIRST
        start_sweep -- never on connect (see open)."""
        if self.d.set_mode:
            self._write("DEV:MODE VNA")                    # VERIFY: LibreVNA only
        self._write("VNA:SWEEP FREQUENCY")
        if self.d.sweep_type:
            # the brain's grid is linear; VNStudio can also sweep LOG
            self._write("VNA:SWEEPTYPE LIN")
        self._write("VNA:ACQ:AVG 1")                       # the brain averages
        if self.d.own_trace and TRACE not in self._trace_list():
            # VERIFY: VNA:TRAC:NEW creates a trace of the default parameter
            self._write(f"VNA:TRAC:NEW {TRACE}")
        self._check("taking over the sweep")
        self._prepared = True
        self._sparam = None                                # re-assert it below

    def _abandon(self) -> None:
        """Undo a half-done open: drop the connection we made (an injected one
        belongs to the caller), close VISA, release the claim. Sends nothing."""
        if self._res is not None and self._rm is not None:
            try:
                self._res.close()
            except Exception:
                pass
            self._res = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None
        self._release()

    def _release(self) -> None:
        lock, self._hwlock = self._hwlock, None
        if lock is not None:
            lock.release()

    def close(self) -> None:
        r, self._res = self._res, None
        if r is None:
            self._release()
            return
        try:
            if self._prepared:
                # Hand the program back sweeping continuously, like we found a
                # front panel -- only if we took it over. Seen working on
                # LibreVNA-GUI 1.6.5.  # VERIFY (VNStudio): SINGLE FALSE alone
                # restarts its sweeping.
                r.write("VNA:ACQ:SINGLE FALSE")
                if self.d.set_mode and self._found_mode not in ("", "VNA"):
                    r.write(f"DEV:MODE {self._found_mode}")    # back to SA / GEN
        except Exception:
            pass                                           # closing must not raise
        finally:
            self._pending = False
            self._prepared = False
            try:
                r.close()
            except Exception:
                pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
                self._rm = None
            self._release()                                # LAST: we owned it until now

    def idn(self) -> str:
        return self._idn

    # ---- sweeping ----------------------------------------------------------------

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        """For these settings: VNStudio's own figure (VNA:ACQ:SWE:TIME?, read
        when the settings were written), or on LibreVNA the measured length of
        the last sweep; else the rule of thumb. Never queries: status() calls it
        ten times a second.

        Why learn it: on the LibreVNA the rule of thumb was 5-10x short (201
        points at 10 kHz IFBW: 0.024 s predicted, 0.12-0.32 s real, 2026-10-26),
        and the brain stamps the fly-scan stream's points with this number."""
        t = self._sweep_time.get((int(points), float(ifbw_Hz)))
        return t if t is not None else model.sweep_time_s(points, ifbw_Hz)

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None) -> None:
        if self._res is None:
            raise RuntimeError(f"{self.d.name} is not connected")
        if sparam not in model.SPARAMS:
            raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sparam!r}")
        if not self._prepared:
            self._take_over()                  # the first sweep asked of this module
        f = np.asarray(freqs_Hz, dtype=float)
        want = {"start": float(f[0]), "stop": float(f[-1]), "points": int(f.size),
                "ifbw": float(ifbw_Hz), "power": float(power_dBm)}
        changed = self._apply(want)
        if self.d.own_trace and sparam != self._sparam:
            self._write(f"VNA:TRAC:PARAM {TRACE} {sparam}")       # VERIFY
            changed = True
        self._sparam = sparam
        if changed:
            self._check("applying the sweep settings")
            self._verify_settings(want)
            if self.d.cal_query:
                self._calibration()
            if self.d.sweep_time_query:
                try:
                    t = float(self._query(self.d.sweep_time_query))
                    if math.isfinite(t) and t > 0:
                        self._sweep_time[(want["points"], want["ifbw"])] = t
                except Exception as exc:
                    self.warnings.append(f"reading the sweep time: {exc}")
        self._freqs = f
        self._ifbw_asked = want["ifbw"]
        # ONE sweep: SINGLE TRUE selects single mode AND starts a new sweep
        # (both guides). A settings write above may already have restarted one;
        # this restarts it again, so the sweep we read began after every change.
        self._write("VNA:ACQ:SINGLE TRUE")
        self._pending = True
        self._started_at = self._clock()

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise RuntimeError("finish_sweep without start_sweep")
        # The brain has already waited out sweep_time_s(). Poll "done?" until it
        # says so, with a ceiling well above the estimate.
        est = self.sweep_time_s(self._freqs.size, self._readback.get("ifbw", 1e3))
        ceiling = max(float(self.cfg.hardware.timeout_s), 3.0 * est + 10.0)
        t0 = self._clock()
        # VERIFY: the done query answers "not done" from the moment SINGLE TRUE
        # has been processed (the program handles commands in order, so the
        # poll comes after the restart -- check a stale "done" never slips in).
        while True:
            reply = self._query(self.d.done_query).upper()
            if reply == self.d.done_reply:
                break
            if self._clock() - t0 > ceiling:
                self._pending = False
                raise TimeoutError(f"{self.d.name} sweep did not finish within "
                                   f"{ceiling:.0f} s ({self.d.done_query} -> {reply!r})")
            self._sleep(0.02)
        self._pending = False
        took = self._clock() - self._started_at
        if not self.d.sweep_time_query and took > 0:
            # trigger -> "done" seen: an upper bound by one poll (20 ms)
            self._sweep_time[(self._freqs.size, self._ifbw_asked)] = took
        name = TRACE if self.d.own_trace else self._sparam
        # VERIFY (VNStudio): the S-parameter's trace must be shown in VNStudio
        # to be measured ("all enabled traces in the current interface").
        x, z = parse_trace(self._query(f"VNA:TRAC:DATA? {name}"))
        self._check_grid(x)
        meta = {"ifbw_actual_Hz": self._readback.get("ifbw", math.nan),
                "power_actual_dBm": self._readback.get("power", math.nan),
                "correction_on": self._correction,   # None: VNStudio has no query
                "calibration": self._cal,
                "sweep_s": took,
                "f_res_model_Hz": math.nan}
        return z, meta

    def abort_sweep(self) -> None:
        """Forget the sweep in flight. Nothing is sent: the next SINGLE TRUE
        restarts the sweep from its first point anyway, and on VNStudio a
        VNA:ACQ:STOP would need a VNA:ACQ:RUN before sweeping again.
        # VERIFY: SINGLE TRUE mid-sweep restarts it on both programs."""
        self._pending = False

    # ---- internals ---------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        self._res.write(cmd)

    def _query(self, cmd: str) -> str:
        reply = str(self._res.query(cmd)).strip()
        # VNStudio answers some failures with a word instead of a value
        if reply.upper() in ("ERROR", "GATEWAY_EXECUTION_ERROR", "SERVER_NOT_READY") \
                and cmd != "DEV:CONN?":
            raise RuntimeError(f"{self.d.program}: {cmd} -> {reply}")
        return reply

    def _calibration(self):
        """The active calibration (LibreVNA: "SOLT_12", ... or "NONE"). A failed
        query leaves both unknown (None) -- it must not stop a sweep."""
        try:
            self._cal = self._query(self.d.cal_query)
            self._correction = self._cal.upper() not in ("", "NONE")
        except Exception as exc:
            self.warnings.append(f"reading the calibration: {exc}")
            self._cal = self._correction = None
        return self._cal

    def _trace_list(self) -> list[str]:
        return [t.strip() for t in self._query("VNA:TRAC:LIST?").split(",") if t.strip()]

    def _esr(self, warn: bool = False) -> list[str]:
        """Read *ESR? -- and any unasked-for text lines in front of its number
        (see the module docstring). Returns the errors found; `warn` also files
        them as warnings (for a read that must not fail)."""
        errs: list[str] = []
        line = str(self._res.query("*ESR?")).strip()
        for _ in range(20):
            if re.fullmatch(r"[+]?\d+", line):
                break
            errs.append(line or "(empty line)")
            line = str(self._res.read()).strip()
        else:
            raise RuntimeError(f"{self.d.program}: no *ESR? number among the replies "
                               f"({' | '.join(errs[-3:])})")
        code = int(line)
        errs += [f"{text} (*ESR? bit {bit})" for bit, text in _ESR_ERRORS.items() if code & bit]
        if warn:
            self.warnings.extend(errs)
        return errs

    def _check(self, doing: str) -> None:
        errs = self._esr()
        if errs:
            self._applied = {}                 # unknown state: re-write next time
            raise RuntimeError(f"{self.d.program} reported errors while {doing}: "
                               + " | ".join(errs))

    def _apply(self, want: dict) -> bool:
        """Write the settings that differ from what the program holds, each
        followed by its read-back. Start/stop in the order that never crosses
        them (moving the band UP writes stop first)."""
        order = ["points", "ifbw", "power"]
        old_stop = self._applied.get("stop")
        if old_stop is not None and want["start"] > old_stop:
            order = ["stop", "start"] + order
        else:
            order = ["start", "stop"] + order
        changed = False
        for k in order:
            v = want[k]
            if self._applied.get(k) == v:
                continue
            template, readback = _SETTERS[k]
            self._write(template.format(v=v))
            got = self._query(readback)
            self._readback[k] = int(float(got)) if k == "points" else float(got)
            self._applied[k] = v
            changed = True
        return changed

    def _verify_settings(self, want: dict) -> None:
        """Refuse a sweep the program did not take (it may move an impossible
        value to the nearest one it can do)."""
        rb = self._readback
        bad = []
        if rb.get("points") != want["points"]:
            bad.append(f"points {rb.get('points')} (asked {want['points']})")
        for k in ("start", "stop"):
            if not math.isclose(rb.get(k, math.nan), want[k], rel_tol=0, abs_tol=1.0):
                bad.append(f"{k} {rb.get(k)} Hz (asked {want[k]:.12g})")
        if bad:
            self._applied = {}
            raise RuntimeError(f"{self.d.name} did not take the sweep: " + "; ".join(bad))

    def _check_grid(self, x: np.ndarray) -> None:
        """Every point's frequency, as the program reports it, against the grid
        the brain files the trace under. Tolerance: 2 Hz or 1 ppb of the
        frequency, whichever is larger -- room for the program rounding to whole
        Hz or printing 10 digits, none for a grid that is really different."""
        f = self._freqs
        if x.size != f.size:
            raise RuntimeError(f"{self.d.name} returned {x.size} points for {f.size}")
        off = np.abs(x - f)
        if np.any(off > np.maximum(2.0, 1e-9 * np.abs(f))):
            worst = float(np.max(off))
            raise RuntimeError(f"{self.d.name} measured at other frequencies than asked "
                               f"(worst point off by {worst:.6g} Hz)")


def LibreVna(cfg: Config, **kw) -> SweepServerVna:
    """The LibreVNA behind LibreVNA-GUI."""
    return SweepServerVna(cfg, LIBREVNA, **kw)


def HarogicVna(cfg: Config, **kw) -> SweepServerVna:
    """The Harogic PNB-60 behind VNStudio."""
    return SweepServerVna(cfg, HAROGIC, **kw)
