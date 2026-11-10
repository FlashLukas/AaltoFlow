"""The real analyser, second make: Copper Mountain's USB analysers driven by
their S2VNA program -- the C1209 (2 ports, 9 GHz; driver "cmt") and the S5085
(2 ports, 8.5 GHz; driver "s5085").

ONE BACKEND, SEVERAL MODELS (2026-11-09): the C1209 (Cobalt) and the S5085
(Planar) are both run by S2VNA and speak the same SCPI (the "S2VNA and S4VNA
SCPI Programming Manual"). What differs is only the data sheet -- frequency,
power, IF bandwidth and point ranges -- the name the analyser gives in *IDN?,
and its fastest point. That is a `CmtModel` below; the SCPI path is shared, so
whatever is verified on one model holds for the other.

This is the ONLY file that talks to the Copper Mountain, and it imports pyvisa
inside `open()`, so the package (and every test) works on a PC without it.
Install with  `uv sync --extra gui --extra real`.

What is different from the Keysight PNA-X, and why this is a file of its own:

  * The C1209 is a USB box with no front panel. The analyser is Copper
    Mountain's program S2VNA running on the PC it is plugged into, and SCPI goes
    to THAT PROGRAM over a TCP socket -- port 5025, which has to be switched on
    once in S2VNA: System > Misc Setup > Network Setup > Socket Server (or start
    it as  S2VNA.exe /SocketServer:on /SocketPort:5025). On the DynaCool setup
    S2VNA runs on the same PC, so the address is 127.0.0.1.

  * pyvisa is used with its pure-Python backend, pyvisa-py ("@py"), which
    speaks raw sockets itself: no NI-VISA or Keysight IO Libraries needed
    (NI-VISA on the lab PC listed nothing when the PNA was tried).

  * Data comes as ASCII. Copper Mountain's manual: "The binary transfer format
    is supported by HiSLIP protocol only." Over a raw socket, REAL,64 is not an
    option; a 1601-point trace is ~60 kB of text, a few ms on localhost.
    (`hardware.data_format = "REAL,64"` is honoured if the resource is HiSLIP.)

  * Triggering is Copper Mountain's documented single-sweep recipe: trigger
    source BUS with continuous initiation ON puts the channel in "waiting for
    trigger"; `TRIG:SING` starts ONE sweep and stays pending until it ends, and
    `*OPC?` answers "1" when it has. This set-up happens at the FIRST sweep
    somebody asks this module for, never on connect: connecting only QUERIES
    (Lukas, 2026-09-27), and the brain adopts the sweep S2VNA already holds.
    Only a sweep the brain triggered, after the
    magnet settled, is ever read -- the same guarantee the PNA backend gets from
    SING/HOLD. On close the trigger source goes back to INTernal, so S2VNA's
    window is left sweeping for whoever uses it next.

  * One trace (trace 1 of channel 1) shows the selected S-parameter. The old
    LabVIEW program read TWO formatted traces (amplitude and phase in degrees)
    and rebuilt the complex number from them; here the complex data comes
    directly (`CALC1:TRAC1:DATA:SDAT?`, "the corrected data array ... S-parameter
    complex values"), so nothing is lost to formatting or phase wrapping.

  * SEVERAL S-parameters from ONE sweep (2026-11-10, Lukas: "S11 and S21 in a
    single go"; config sweep.extra_sparams). Every trace of a channel is
    filled by the same sweep, so channel 1 gets one trace per parameter
    (`CALC1:PAR:COUN n`, `CALC1:PAR<i>:DEF Sxx`, trace 1 = the primary), ONE
    TRIG:SING + *OPC? as before, then `CALC1:TRAC<i>:DATA:SDAT?` for each.
    S11 and S21 both come from the forward sweep: no extra time. Adding S12
    or S22 makes S2VNA sweep the reverse direction too (twice the time),
    which the brain accounts for.
    VERIFIED on the S5085 (2026-11-10, S11 + S21, S2VNA 24.3.4): one sweep per
    acquisition, and both traces equal S2VNA's Tr1 / Tr2 markers point for
    point (docs/VERIFIED_INSTRUMENTS.md). Not yet run there: S11 + S22
    (forward + reverse) and going back from several traces to one.

  * No calibration handling: the setup runs uncorrected for now (Lukas,
    2026-09-25), and u / ln(S/S_ref) divide the cables out anyway. Whether
    S2VNA has a correction on is still reported with every sample.

UNTESTED ON THE INSTRUMENT. The commands follow the "S2VNA and S4VNA SCPI
Programming Manual" (Copper Mountain, rev. 20.3); every line not yet seen
working on THIS analyser is marked `# VERIFY` with what to check. As with the
PNA, a wrong command does not stop S2VNA, it only queues an error -- so the
error queue is drained after configuring and anything in it is refused.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from .. import model
from ..config import Config
from . import claim as hwclaim      # the one-service-per-analyser lock
from ..field import FieldReading
from .pna import parse_sdata

#: the channel and trace this backend owns
CH, TR = 1, 1

#: The C1209's frequency range (Copper Mountain data sheet: 100 kHz - 9 GHz).
#: `limit_envelope` pulls the config's envelope inside it.  # VERIFY on the unit
C1209_MIN_HZ = 100e3
C1209_MAX_HZ = 9e9


@dataclass(frozen=True)
class CmtModel:
    """What differs between Copper Mountain models behind S2VNA."""

    driver: str              # hardware.driver
    name: str                # for labels and messages
    idn_token: str           # what *IDN? names ("CMT,<token>,<serial>,...")
    #: data-sheet limits -> cfg.limits keys (librevna.limit_envelope's rule:
    #: a limit at the generic default is REPLACED, one set in vna.ini narrowed)
    envelope: dict
    #: the analyser's fastest point (data sheet), a floor under the rule-of-thumb
    #: sweep time -- at a wide IFBW 1.2/IFBW per point is faster than the box
    min_point_s: float = 0.0


C1209 = CmtModel(
    driver="cmt", name="Copper Mountain C1209", idn_token="C1209",
    # only the frequency range is taken from the data sheet so far
    envelope={"freq_min_Hz": C1209_MIN_HZ, "freq_max_Hz": C1209_MAX_HZ},
)

S5085 = CmtModel(
    driver="s5085", name="Copper Mountain S5085", idn_token="S5085",
    # "S5085 Extended Specification Sheet" (CMT21, coppermountaintech.com):
    # 9 kHz - 8.5 GHz; power -55 .. +5 dBm up to 8 GHz (+3 dBm from 8.0 to
    # 8.5 GHz -- S2VNA moves a setting it cannot make to its limit and the
    # read-back after every write reports where it went); IF bandwidths 1 Hz
    # - 100 kHz; 2 - 200 001 points; 70 us per point typ. (End of sale 2024,
    # serviced; same S2VNA as the C1209.)  # VERIFY on the unit
    envelope={"freq_min_Hz": 9e3, "freq_max_Hz": 8.5e9,
              "power_min_dBm": -55.0, "power_max_dBm": 5.0,
              "ifbw_min_Hz": 1.0, "ifbw_max_Hz": 100e3,
              "points_max": 200001},
    min_point_s=70e-6,
)

#: hardware.driver -> model
MODELS = {m.driver: m for m in (C1209, S5085)}


def model_for(cfg: Config) -> CmtModel:
    """The model `hardware.driver` names ("cmt" = the C1209, as before)."""
    return MODELS.get(cfg.hardware.driver, C1209)

_SETTERS = {
    # key: (write template, read-back query). Hz, points, Hz, dBm.
    "start": (":SENS{ch}:FREQ:STAR {v:.12g}", ":SENS{ch}:FREQ:STAR?"),
    "stop": (":SENS{ch}:FREQ:STOP {v:.12g}", ":SENS{ch}:FREQ:STOP?"),
    "points": (":SENS{ch}:SWE:POIN {v:d}", ":SENS{ch}:SWE:POIN?"),
    "ifbw": (":SENS{ch}:BWID {v:.12g}", ":SENS{ch}:BWID?"),
    # "Sets or reads out the power level for the frequency sweep type" --
    # resolution 0.05 dBm, out-of-range values are moved to the nearest limit
    # (the read-back shows where it went).  # VERIFY: port coupling for S12/S22
    "power": (":SOUR{ch}:POW {v:.6g}", ":SOUR{ch}:POW?"),
}


def limit_envelope(cfg: Config, cmt_model: CmtModel | None = None) -> None:
    """Make the config's envelope what this model can do, so a setpoint the
    analyser cannot reach is clamped (and announced) by the brain instead of
    being silently moved by S2VNA.

    The rule is LibreVNA's (librevna.limit_envelope): a limit still at the
    generic default (a PNA-shaped 10 MHz - 20 GHz) is REPLACED by the data
    sheet, one set in vna.ini is only narrowed. (Before 2026-11-09 the C1209's
    envelope was only ever narrowed, which kept its 100 kHz end behind the
    default's 10 MHz; an S5085 would have lost everything below 10 MHz.)"""
    from .librevna import limit_envelope as _envelope   # pure Python, no pyvisa
    _envelope(cfg, cmt_model or model_for(cfg))


class CmtVna:
    simulated = False
    #: several S-parameters from one sweep: one S2VNA trace each (base.py)
    multi_sparams = True

    # FLY-SCAN TIMING (the brain's stream, analyzer.py, 2026-10-09). The brain
    # stamps a sweep as starting when start_sweep returns plus this, and gives
    # point i the moment t_start + (i + 0.5) * T / n with T = sweep_time_s()
    # -- here only the rule of thumb, because S2VNA has no sweep-time query in
    # the manual. All of it is UNVERIFIED on the C1209:
    # VERIFY: the delay between `:TRIG:SING` and the first point -- measure it
    #   and put it here;
    # VERIFY: the real sweep time (finish_sweep's meta `sweep_s` measures
    #   trigger -> *OPC? done, an upper bound): if it differs from the rule of
    #   thumb, sweep_time_s() must return the measured figure, or every point
    #   channel is stamped at the wrong moment;
    # VERIFY: that the points are evenly spaced in time across the sweep.
    trigger_latency_s = 0.0

    def __init__(self, cfg: Config, resource=None, clock=time.monotonic, sleep=time.sleep,
                 cmt_model: CmtModel | None = None):
        """`resource` = an already-open VISA resource. Tests pass a FAKE one
        (anything with write / query / query_ascii_values / query_binary_values
        / close); normally it is None and `open()` opens `hardware.cmt_resource`.
        `cmt_model` = which analyser (default: from hardware.driver)."""
        self.cfg = cfg
        self.model = cmt_model or model_for(cfg)
        self._res = resource
        self._rm = None
        self._hwlock = None               # our claim on the analyser (see open)
        self._clock = clock
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # what the instrument is known to hold
        self._readback: dict = {}         # what it reported back for those
        self._sparams = None              # the traces we defined, trace 1 first
        self._count = None                # :CALC1:PAR:COUN as we last set it
        self._correction = None
        self._pending = False
        # Sweeps WE aborted since the last error check: S2VNA logs each as
        # '-239,"TRIG:SING interrupted"' (see abort_sweep)
        self._aborts = 0
        self._prepared = False            # set up for single sweeps yet? (see _take_over)
        self._points = 0
        self._started_at = math.nan
        self.warnings: list[str] = []

    # ---- lifecycle -------------------------------------------------------------

    def _binary(self) -> bool:
        """Binary transfer only if asked for AND the link is HiSLIP (manual 5.6)."""
        hw = self.cfg.hardware
        return (not hw.data_format.upper().startswith("ASCII")
                and "HISLIP" in hw.cmt_resource.upper())

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
            # ONE SERVICE PER ANALYSER (Lukas, 2026-09-27): claim the address
            # BEFORE the connection is opened. Another service holding it ->
            # hwlock.HardwareBusy here, and not one byte reaches the analyser.
            self._hwlock = hwclaim.claim(hw.cmt_resource, self._rm)
            self._res = self._rm.open_resource(hw.cmt_resource)
        else:
            # an injected resource (the tests' fake analyser) is claimed the same way
            self._hwlock = hwclaim.claim(hw.cmt_resource)
        r = self._res
        r.timeout = int(float(hw.timeout_s) * 1000)       # pyvisa counts milliseconds
        # A raw socket has no end-of-message marker: every command MUST end in
        # a newline, and so does every reply (manual section 2.7).
        r.read_termination = "\n"
        r.write_termination = "\n"
        self._applied, self._readback, self._sparams, self._count = {}, {}, None, None

        self._prepared = False
        # CONNECT = LOOK (Lukas, 2026-09-27: read the state at start, change
        # nothing). Queries and *CLS only; S2VNA keeps sweeping as it was
        # until somebody asks THIS module for a trace (`_take_over`).
        self._idn = r.query("*IDN?").strip()               # "CMT,C1209,<serial>,<sw>/<hw>"
        if self.model.idn_token.upper() not in self._idn.upper():
            # a warning, not a refusal: S2VNA is the same program for every
            # model, only the envelope (hardware.driver) would be wrong
            self.warnings.append(f"expected a {self.model.idn_token} (hardware.driver = "
                                 f"{self.model.driver!r}), the analyser says {self._idn!r}")
        self._write("*CLS")                                # start with an empty error queue

    def read_state(self) -> dict:
        """What S2VNA is doing now, by queries only (see base.read_state). A
        query it does not know becomes a warning and its key is left out. The
        values read are what the analyser HOLDS, so the first sweep writes only
        what the brain changed."""
        r = self._res
        st: dict = {}
        for key, setter, cast in (("start_Hz", "start", float), ("stop_Hz", "stop", float),
                                  ("points", "points", int), ("ifbw_Hz", "ifbw", float),
                                  ("power_dBm", "power", float)):
            try:
                v = cast(float(r.query(_SETTERS[setter][1].format(ch=CH)).strip()))
            except Exception as exc:
                self.warnings.append(f"reading {key}: {exc}")
                continue
            st[key] = v
            self._applied[setter] = v
            self._readback[setter] = v
        try:
            p = r.query(f":CALC{CH}:PAR{TR}:DEF?").strip().upper()     # VERIFY: e.g. "S21"
            st["sparam"] = p if p in model.SPARAMS else None
        except Exception as exc:
            self.warnings.append(f"reading the measured S-parameter: {exc}")
            st["sparam"] = None
        try:
            # informative only: how many traces S2VNA shows. What THIS module
            # measures per sweep is its own config (sweep.extra_sparams) --
            # the screen layout is a display choice, not a scan design.
            st["trace_count"] = int(float(r.query(f":CALC{CH}:PAR:COUN?").strip()))  # VERIFY
        except Exception as exc:
            self.warnings.append(f"reading the trace count: {exc}")
        try:
            st["trigger_source"] = r.query(":TRIG:SOUR?").strip().upper()   # VERIFY: INT|EXT|MAN|BUS
        except Exception as exc:
            self.warnings.append(f"reading the trigger source: {exc}")
        st["averaging_on"] = self._query_bool(f":SENS{CH}:AVER?")        # VERIFY: 0/1 reply
        st["correction_on"] = self._query_bool(f":SENS{CH}:CORR:STAT?")
        # Driving S2VNA means taking over its trigger (a change), so the brain
        # starts hands-off.
        st["continuous"] = False
        self.warnings.extend(self._errors())               # reading the queue changes nothing
        return st

    def _take_over(self) -> None:
        """Set S2VNA up for single sweeps on demand. Runs at the FIRST
        start_sweep, i.e. only when somebody asks this module to measure --
        never on connect (see open)."""
        # one trace on channel 1, active, showing what we measure (more,
        # one per S-parameter, if several are asked for: _ensure_sparams)
        self._write(f":CALC{CH}:PAR:COUN 1")
        self._count = 1
        self._write(f":CALC{CH}:PAR{TR}:SEL")
        self._write(f":SENS{CH}:AVER OFF")                 # the brain averages
        if self._binary():
            self._write(":FORM:DATA REAL")                 # VERIFY: 64-bit doubles
            self._write(":FORM:BORD SWAP")                 # little-endian, the PC's own
        else:
            self._write(":FORM:DATA ASC")
        # Single sweeps on demand: BUS trigger + continuous initiation = the
        # channel waits for our TRIG:SING (manual 5.1, example 1).
        self._write(":TRIG:SOUR BUS")
        self._write(f":INIT{CH}:CONT ON")
        self._check_errors("taking over the sweep")
        self._prepared = True

    def _abandon(self) -> None:
        """Undo a half-done open: drop the connection we made (an injected
        resource belongs to the caller), close VISA, release the claim. Sends
        nothing -- the analyser was never taken over."""
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
            self._release()                            # e.g. after a failed open
            return
        try:
            if self._pending:
                r.write(":ABOR")
            if self._prepared:
                # Hand S2VNA back sweeping on its own, like a front panel --
                # only if we took it over; one we only looked at stays as it was.
                r.write(":TRIG:SOUR INT")
        except Exception:
            pass                                           # closing must not raise on a dead link
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
            # released LAST, after the connection is closed: until then this
            # service still owns the analyser
            self._release()

    def idn(self) -> str:
        return self._idn

    # ---- sweeping ----------------------------------------------------------------

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        """The rule of thumb (~1.2 / IFBW per point), never faster than the
        model's data-sheet time per point. S2VNA has no sweep-time query in the
        manual, so this is only the brain's first wait; the real end of the
        sweep is `*OPC?`. Never queries (status() calls it)."""
        return max(model.sweep_time_s(points, ifbw_Hz), int(points) * self.model.min_point_s)

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None,
                    sparams=None) -> None:
        """`sparams` (optional) = every S-parameter this ONE sweep returns, the
        primary `sparam` first: one S2VNA trace each, read by finish_sweep."""
        wanted = tuple(sparams) if sparams else (sparam,)
        if wanted[0] != sparam:
            raise ValueError(f"sparams must start with the primary {sparam}, got {wanted}")
        if self._res is None:
            raise RuntimeError(f"{self.model.idn_token} is not connected")
        first = not self._prepared
        if first:
            self._take_over()                  # the first sweep asked of this module
        f = np.asarray(freqs_Hz, dtype=float)
        want = {"start": float(f[0]), "stop": float(f[-1]), "points": int(f.size),
                "ifbw": float(ifbw_Hz), "power": float(power_dBm)}
        # `first`: even with nothing to write, check the grid and learn the
        # correction state once
        changed = self._apply(want) or first
        if wanted != self._sparams:
            self._ensure_sparams(wanted)
            changed = True
        if changed:
            self._check_errors("applying the sweep settings")
            self._verify_grid(want)
            self._correction = self._query_bool(f":SENS{CH}:CORR:STAT?")
        self._points = want["points"]
        # ONE sweep. TRIG:SING returns at once on the wire and stays pending
        # until the sweep ends; finish_sweep's *OPC? waits for that.
        # VERIFY: the channel is in "waiting for trigger" here (TRIG:STAT? ->
        # WTRG); a TRIG:SING in any other state is refused with an error.
        self._write(":TRIG:SING")
        self._pending = True
        self._started_at = self._clock()

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise RuntimeError("finish_sweep without start_sweep")
        r = self._res
        # The brain has already waited out the predicted sweep time; *OPC?
        # blocks until the sweep really ended. Give it a ceiling well above
        # the rule of thumb, in case the estimate is short for this analyser.
        est = self.sweep_time_s(self._points, self._readback.get("ifbw", 1e3))
        ceiling = max(float(self.cfg.hardware.timeout_s), 3.0 * est + 10.0)
        old = r.timeout
        r.timeout = int(ceiling * 1000)
        try:
            done = r.query("*OPC?").strip()               # VERIFY: "1" at sweep end
        except Exception as exc:
            self._pending = False
            raise TimeoutError(f"{self.model.idn_token} sweep did not finish within {ceiling:.0f} s ({exc})")
        finally:
            r.timeout = old
        self._pending = False
        if not done.endswith("1"):
            raise RuntimeError(f"{self.model.idn_token}: unexpected *OPC? reply {done!r}")

        # every trace of channel 1 came from the sweep that just ended
        traces = {sp: self._read_trace(i) for i, sp in enumerate(self._sparams, start=1)}
        z = traces[self._sparams[0]]
        meta = {"ifbw_actual_Hz": self._readback.get("ifbw", math.nan),
                "power_actual_dBm": self._readback.get("power", math.nan),
                "correction_on": self._correction,
                "sweep_s": self._clock() - self._started_at,
                "f_res_model_Hz": math.nan}
        if len(traces) > 1:
            meta["traces"] = traces
        return z, meta

    def _read_trace(self, i: int) -> np.ndarray:
        """Trace i of channel 1 as complex numbers (SDAT = corrected re, im pairs)."""
        r = self._res
        query = f":CALC{CH}:TRAC{i}:DATA:SDAT?"
        if self._binary():
            data = r.query_binary_values(query, datatype="d", is_big_endian=False,
                                         container=np.array)
        else:
            data = r.query_ascii_values(query, container=np.array)
        return parse_sdata(data, self._points)

    def abort_sweep(self) -> None:
        if self._pending and self._res is not None:
            self._pending = False
            # With continuous initiation on, ABOR puts the channel back into
            # "waiting for trigger" (manual: ABOR), ready for the next TRIG:SING.
            # Checked on the S5085 (2026-11-10): it is -- and S2VNA then logs
            # '-239,"TRIG:SING interrupted"' for the trigger we interrupted.
            # Ours, not a fault: _check_errors drops exactly that many -239s.
            # (A settings change in continuous mode aborts the running sweep;
            # the next settings check reported nine of them as a hardware
            # failure and the scan stopped.)
            self._write(":ABOR")
            self._aborts += 1
        self._pending = False

    # ---- internals ---------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        self._res.write(cmd)

    def _query_bool(self, cmd: str):
        try:
            return self._res.query(cmd).strip() in ("1", "ON", "+1")
        except Exception:
            return None

    def _apply(self, want: dict) -> bool:
        """Write the settings that differ from what the instrument holds, each
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
            self._write(template.format(ch=CH, v=v))
            got = self._res.query(readback.format(ch=CH)).strip()
            self._readback[k] = int(float(got)) if k == "points" else float(got)
            self._applied[k] = v
            changed = True
        return changed

    def _verify_grid(self, want: dict) -> None:
        """Refuse a frequency grid the analyser did not take: every trace would
        be filed under frequencies it was not measured at."""
        rb = self._readback
        bad = []
        if rb.get("points") != want["points"]:
            bad.append(f"points {rb.get('points')} (asked {want['points']})")
        for k in ("start", "stop"):
            if not math.isclose(rb.get(k, math.nan), want[k], rel_tol=0, abs_tol=1.0):
                bad.append(f"{k} {rb.get(k)} Hz (asked {want[k]:.12g})")
        if bad:
            self._applied = {}                     # force a re-write next time
            raise RuntimeError(f"{self.model.idn_token} did not take the sweep: " + "; ".join(bad))

    def _ensure_sparams(self, sparams: tuple) -> None:
        """One trace per S-parameter on channel 1, trace 1 = the primary.

        The trace count is written only when it changes, so a one-parameter
        module sends exactly what it always did (DEF + SEL on trace 1)."""
        for sp in sparams:
            if sp not in model.SPARAMS:
                raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sp!r}")
        if len(set(sparams)) != len(sparams):
            raise ValueError(f"an S-parameter is listed twice: {sparams}")
        if len(sparams) != self._count:
            # Every trace is re-defined below, so what S2VNA puts in a new
            # trace does not matter (2 -> worked on the S5085, 2026-11-10).
            # VERIFY: shrinking (several -> 1) on the instrument
            self._write(f":CALC{CH}:PAR:COUN {len(sparams)}")
            self._count = len(sparams)
        for i, sp in enumerate(sparams, start=1):
            self._write(f":CALC{CH}:PAR{i}:DEF {sp}")
        self._write(f":CALC{CH}:PAR{TR}:SEL")
        self._check_errors("selecting " + ", ".join(sparams))
        self._sparams = tuple(sparams)

    def _errors(self) -> list[str]:
        """Drain the error queue ("0, No error" = empty; at most 100 entries)."""
        out = []
        for _ in range(101):
            e = self._res.query(":SYST:ERR?").strip()
            try:
                code = int(e.split(",", 1)[0])
            except ValueError:
                code = None                        # unparseable: report it as it is
            if code == 0:
                break
            out.append(e)
        return out

    def _check_errors(self, doing: str) -> None:
        errs = self._errors()
        # drop one -239 "interrupted" per sweep WE aborted -- no more: an
        # interruption we did not cause is still reported
        ours, self._aborts = self._aborts, 0
        kept = []
        for e in errs:
            if ours > 0 and e.split(",", 1)[0].strip() == "-239":
                ours -= 1
                continue
            kept.append(e)
        errs = kept
        if errs:
            raise RuntimeError(f"{self.model.idn_token} reported errors while {doing}: " + " | ".join(errs))
