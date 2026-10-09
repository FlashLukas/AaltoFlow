"""The real analyser: a Keysight PNA-X N5222A, over VISA (pyvisa).

This is the ONLY file that talks to the instrument, and it imports pyvisa inside
`open()`, so the package (and every test) works on a PC without pyvisa or a
VISA library. Install on the lab PC with  `uv sync --extra gui --extra real`.

UNTESTED ON THE INSTRUMENT. The command sequence below follows the Keysight PNA
SCPI reference and the SCPI the old LabVIEW program ("RotSampleInVNA") sent,
but every line that has not been seen working on THIS analyser is marked
`# VERIFY` with what to check. Do the hardware pass with the PNA's own
"SCPI Errors" window open (or read `SYST:ERR?`): a wrong command does not stop
the instrument, it only puts a line in its error queue -- which is why this
backend drains that queue after configuring and refuses to carry on if it holds
anything.

How it is driven, and why:

  * ONE named measurement, 'AALTOFLOW_S', on channel 1, showing the selected
    S-parameter. Named, so it never collides with (or deletes) the
    measurements someone set up on the front panel; the backend only ever
    touches its own.

  * CONNECTING CHANGES NOTHING (Lukas, 2026-09-27: every module reads the
    instrument's state at start and adopts it). `open()` only queries;
    `read_state()` reports the sweep the PNA holds, and the brain takes it
    over as its own settings. The set-up below happens at the FIRST sweep
    somebody asks this module for (acquire, take_reference, continuous on).

  * SINGLE sweeps: the channel is put in HOLD at that first sweep, and every sweep is
    `SENS1:SWE:MODE SING`. A free-running analyser hands back whatever sweep
    happened to be on screen -- possibly one that started BEFORE the magnet
    arrived. A single sweep triggered by the brain is the only trace that is
    provably "after the field settled". On disconnect the channel goes back to
    CONTINUOUS, so the front panel is left alive for whoever uses it next.

  * Instrument averaging OFF; the brain averages N single sweeps coherently
    (complex mean). Same meaning of "averages" on the simulator and the real
    analyser, and the brain can abandon an average half-way when a setting
    changes.

  * Data as REAL,64 (binary doubles), not ASCII. A 1601-point complex trace is
    3202 numbers: 25.6 kB in binary against ~60 kB of text, sent exactly
    (ASCII rounds to the printed digits) and with no text parsing. The byte
    order is set explicitly (`FORM:BORD SWAP` = little-endian, the PC's own),
    because a wrong byte order does not fail -- it returns garbage numbers of
    the right length. `hardware.data_format = "ASCII"` switches to what the
    LabVIEW code used, as a fallback if the binary block misbehaves.

  * Settings are written only when they CHANGE, then read back. The PNA snaps
    some values to what it supports (IF bandwidth goes to the nearest 1-1.5-2-
    3-5-7 step) and silently limits others; the read-back is what the sample is
    filed with, and a frequency grid that does not match the brain's is refused
    rather than stored under the wrong frequencies.
"""

from __future__ import annotations

import math
import time

import numpy as np

from .. import model
from ..config import Config
from . import claim as hwclaim      # the one-service-per-analyser lock
from ..field import FieldReading

#: the measurement this backend owns on the instrument
MEAS = "AALTOFLOW_S"
#: the channel it lives on
CH = 1

_SETTERS = {
    # key: (write template, read-back query). Hz, points, Hz, dBm.
    "start": ("SENS{ch}:FREQ:STAR {v:.12g}", "SENS{ch}:FREQ:STAR?"),
    "stop": ("SENS{ch}:FREQ:STOP {v:.12g}", "SENS{ch}:FREQ:STOP?"),
    "points": ("SENS{ch}:SWE:POIN {v:d}", "SENS{ch}:SWE:POIN?"),
    "ifbw": ("SENS{ch}:BAND {v:.12g}", "SENS{ch}:BAND?"),
    # VERIFY: source power of port 1. With port power COUPLED (the default)
    # this sets every port; check `SOUR1:POW:COUP?` and the power level shown
    # for port 2 when measuring S12/S22.
    "power": ("SOUR{ch}:POW1:LEV:IMM:AMPL {v:.6g}", "SOUR{ch}:POW1:LEV:IMM:AMPL?"),
}


class PnaVna:
    simulated = False

    # FLY-SCAN TIMING (the brain's stream, analyzer.py, 2026-10-09). The brain
    # stamps a sweep as starting when start_sweep returns plus this, and gives
    # point i the moment t_start + (i + 0.5) * T / n with T = sweep_time_s()
    # (the instrument's own SENS:SWE:TIME? once read). All three assumptions
    # are UNVERIFIED on the PNA-X:
    # VERIFY: the delay between `SENS1:SWE:MODE SING` and the first point
    #   (trigger latency, source settling) -- measure it (e.g. sweep a known
    #   field ramp twice in opposite directions and line the dips up) and put
    #   it here;
    # VERIFY: that SENS:SWE:TIME? is the time from the first to the last point
    #   (not including retrace / band-crossing pauses), and that the points are
    #   evenly spaced in time over it (a stepped sweep with dwell and band
    #   crossings is not; SENS:SWE:TYPE / the sweep-time mode decide);
    # VERIFY: back-to-back single sweeps: the dead time between two
    #   (data transfer + re-arm) only leaves gaps between samples, it does not
    #   shift time stamps -- but check nothing is re-armed during a sweep.
    trigger_latency_s = 0.0

    def __init__(self, cfg: Config, resource=None, clock=time.monotonic, sleep=time.sleep):
        """`resource` = an already-open VISA resource. Tests pass a FAKE one
        (anything with write / query / query_binary_values / query_ascii_values
        / close); normally it is None and `open()` opens `hardware.visa_resource`."""
        self.cfg = cfg
        self._res = resource
        self._rm = None
        self._hwlock = None               # our claim on the analyser (see open)
        self._clock = clock
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # what the instrument is known to hold
        self._readback: dict = {}         # what it reported back for those
        self._sparam = None
        self._sweep_time = (None, math.nan)   # ((points, ifbw), seconds)
        self._correction = None
        self._pending = False
        self._prepared = False            # set up for single sweeps yet? (see _take_over)
        self._points = 0
        self.warnings: list[str] = []     # non-fatal instrument complaints (display)

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
        if self._res is None:
            try:
                import pyvisa                      # lazy: only the real backend needs it
            except ImportError as exc:
                raise RuntimeError(
                    "pyvisa is not installed. In vna-control run: "
                    "uv sync --extra gui --extra real") from exc
            self._rm = pyvisa.ResourceManager()
            # ONE SERVICE PER ANALYSER (Lukas, 2026-09-27): claim the address
            # BEFORE the connection is opened. Another service holding it ->
            # hwlock.HardwareBusy here, and not one byte reaches the analyser.
            self._hwlock = hwclaim.claim(hw.visa_resource, self._rm)
            self._res = self._rm.open_resource(hw.visa_resource)
        else:
            # an injected resource (the tests' fake analyser) is claimed the same way
            self._hwlock = hwclaim.claim(hw.visa_resource)
        r = self._res
        r.timeout = int(float(hw.timeout_s) * 1000)       # pyvisa counts milliseconds
        r.read_termination = "\n"
        r.write_termination = "\n"
        self._applied, self._readback, self._sparam = {}, {}, None
        self._prepared = False

        # CONNECT = LOOK. Lukas's rule (2026-09-27): a module reads the
        # instrument at start and changes nothing. So only queries here (and
        # *CLS, which just empties the error queue). The front panel keeps
        # sweeping exactly as it was until somebody asks THIS module for a
        # trace -- then `_take_over` sets up single sweeps (first start_sweep).
        self._idn = r.query("*IDN?").strip()
        self._write("*CLS")                                # start with an empty error queue

    def read_state(self) -> dict:
        """What the PNA is doing now, by queries only (see base.read_state).

        Each query stands on its own: one the firmware does not know is noted
        in `warnings` and its key is left out (the brain then keeps its config
        value), instead of failing the connection. The values read are also
        what the instrument is known to HOLD, so the first sweep writes only
        what the brain really changed."""
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
        st["sparam"] = self._read_sparam()
        try:
            st["sweep_mode"] = r.query(f"SENS{CH}:SWE:MODE?").strip().upper()
        except Exception as exc:
            self.warnings.append(f"reading the sweep mode: {exc}")
        st["averaging_on"] = self._query_bool(f"SENS{CH}:AVER?")      # VERIFY: 0/1 reply
        st["correction_on"] = self._query_bool(f"SENS{CH}:CORR:STAT?")
        # A real analyser is never swept "for free": driving it means taking
        # over its trigger, a change. So the brain starts hands-off.
        st["continuous"] = False
        # Reading the error queue changes nothing; a query this firmware did
        # not know shows up here, as a warning, not as a failed connection.
        self.warnings.extend(self._errors())
        return st

    def _read_sparam(self):
        """The S-parameter shown by our own measurement if it exists from an
        earlier run, else by the SELECTED measurement of channel 1; None if
        neither is an S-parameter (a receiver ratio, say)."""
        r = self._res
        try:
            cat = r.query(f"CALC{CH}:PAR:CAT:EXT?").strip().strip('"')
            items = cat.split(",") if cat and cat.upper() != "NO CATALOG" else []
            pairs = dict(zip(items[0::2], items[1::2]))
            # VERIFY: CALC1:PAR:SEL? returns the (quoted) name of the selected measurement
            name = MEAS if MEAS in pairs else r.query(f"CALC{CH}:PAR:SEL?").strip().strip('"')
            p = pairs.get(name, "").strip().upper()
            return p if p in model.SPARAMS else None
        except Exception as exc:
            self.warnings.append(f"reading the measured S-parameter: {exc}")
            return None

    def _take_over(self) -> None:
        """Set the PNA up for single sweeps on demand. Runs at the FIRST
        start_sweep, i.e. only when somebody asks this module to measure --
        never on connect (see open)."""
        self._write(f"SENS{CH}:SWE:MODE HOLD")             # VERIFY: channel 1 stops sweeping
        self._write("TRIG:SOUR IMM")                       # VERIFY: needed so SING starts at once
        self._write(f"SENS{CH}:AVER OFF")                  # the brain averages
        if self.cfg.hardware.data_format.upper().startswith("ASCII"):
            self._write("FORM:DATA ASCII,0")
        else:
            self._write("FORM:DATA REAL,64")
            self._write("FORM:BORD SWAP")                  # VERIFY: little-endian doubles
        cal = self.cfg.hardware.cal_set.strip()
        if cal:
            # ",0" = activate the correction but do NOT load the cal set's own
            # stimulus: the brain writes start/stop/points itself right after.
            # VERIFY: the old VI used ",1". If the brain's sweep differs from the
            # cal's, the PNA interpolates or switches correction off -- check
            # `SENS1:CORR:STAT?` (reported as correction_on in every sample).
            self._write(f"SENS{CH}:CORR:CSET:ACT '{cal}',0")
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
                r.write("ABOR")
            if self._prepared:
                # hand the front panel back, sweeping -- only if we ever took it
                # over; an analyser we only looked at is left exactly as it was
                r.write(f"SENS{CH}:SWE:MODE CONT")
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

    def cal_sets(self) -> list[str]:
        """Names of the calibration sets on the instrument (for the hardware pass).
        VERIFY: `SENS:CORR:CSET:CAT? NAME` returns a quoted, comma-separated list."""
        raw = self._res.query(f"SENS{CH}:CORR:CSET:CAT? NAME").strip().strip('"')
        return [s for s in raw.split(",") if s]

    # ---- sweeping ----------------------------------------------------------------

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        """The instrument's own figure once it has reported one for these
        settings; the rule of thumb before that. Never queries (status() calls it)."""
        key, t = self._sweep_time
        if key == (int(points), float(ifbw_Hz)) and math.isfinite(t):
            return t
        return model.sweep_time_s(points, ifbw_Hz)

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None) -> None:
        if self._res is None:
            raise RuntimeError("PNA is not connected")
        first = not self._prepared
        if first:
            # first sweep asked of this module: take over, and make our own
            # measurement BEFORE any SENS1 write (a PNA channel with no
            # measurement on it does not exist, and would refuse the settings)
            self._take_over()
            self._ensure_measurement(sparam)
        f = np.asarray(freqs_Hz, dtype=float)
        want = {"start": float(f[0]), "stop": float(f[-1]), "points": int(f.size),
                "ifbw": float(ifbw_Hz), "power": float(power_dBm)}
        # `first`: even with nothing to write, check the grid and learn the
        # correction state and sweep time once
        changed = self._apply(want) or first
        if sparam != self._sparam:
            self._ensure_measurement(sparam)
            changed = True
        if changed:
            self._check_errors("applying the sweep settings")
            self._verify_grid(want)
            self._correction = self._query_bool(f"SENS{CH}:CORR:STAT?")   # VERIFY
            t = float(self._res.query(f"SENS{CH}:SWE:TIME?"))             # seconds
            self._sweep_time = ((want["points"], want["ifbw"]), t)
        self._points = want["points"]
        # Start ONE sweep. The channel was in HOLD; after the sweep it returns
        # to HOLD by itself, which is what finish_sweep waits for.
        self._write(f"SENS{CH}:SWE:MODE SING")            # VERIFY: returns at once (overlapped)
        self._pending = True

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise RuntimeError("finish_sweep without start_sweep")
        r = self._res
        # The brain has already waited out the predicted sweep time; here we
        # wait for the instrument to SAY it is done, with a generous ceiling.
        key, t = self._sweep_time
        deadline = self._clock() + max(float(self.cfg.hardware.timeout_s),
                                       2 * (t if math.isfinite(t) else 0.0) + 5.0)
        while True:
            # VERIFY: during a single sweep the mode reads SING and turns back
            # into HOLD when it completes. (Alternative if not: `*OPC?` right
            # after SING, with a VISA timeout longer than the sweep.)
            mode = r.query(f"SENS{CH}:SWE:MODE?").strip().upper()
            if mode.startswith("HOLD"):
                break
            if self._clock() > deadline:
                self._pending = False
                raise TimeoutError(f"PNA sweep did not finish (mode {mode!r})")
            self._sleep(0.02)
        self._pending = False

        self._write(f"CALC{CH}:PAR:SEL '{MEAS}'")
        # VERIFY: `CALC1:DATA? SDATA` = the selected measurement's complex data,
        # interleaved re, im per point (newer firmware also accepts
        # `CALC1:MEAS<n>:DATA:SDATA?`). It is the CORRECTED data if correction is on.
        query = f"CALC{CH}:DATA? SDATA"
        if self.cfg.hardware.data_format.upper().startswith("ASCII"):
            data = r.query_ascii_values(query, container=np.array)
        else:
            data = r.query_binary_values(query, datatype="d", is_big_endian=False,
                                         container=np.array)
        z = parse_sdata(data, self._points)
        meta = {"ifbw_actual_Hz": self._readback.get("ifbw", math.nan),
                "power_actual_dBm": self._readback.get("power", math.nan),
                "correction_on": self._correction,
                "f_res_model_Hz": math.nan}
        return z, meta

    def abort_sweep(self) -> None:
        if self._pending and self._res is not None:
            self._pending = False
            self._write("ABOR")                            # VERIFY: stops the sweep in progress
            self._write(f"SENS{CH}:SWE:MODE HOLD")
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
        """Write the settings that differ from what the instrument holds.

        Start and stop are written in the order that never makes them cross:
        moving the band UP writes stop first, moving it DOWN writes start
        first. (The PNA would shove the other end along otherwise, and the
        read-back would then have to catch it.)"""
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
        """Refuse a frequency grid the instrument did not take: every trace would
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
            raise RuntimeError("PNA did not take the sweep: " + "; ".join(bad))

    def _ensure_measurement(self, sparam: str) -> None:
        """Make 'AALTOFLOW_S' exist, show `sparam`, and be the selected measurement."""
        if sparam not in model.SPARAMS:
            raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sparam!r}")
        r = self._res
        # VERIFY: the catalog is one quoted string "name,param,name,param,...".
        cat = r.query(f"CALC{CH}:PAR:CAT:EXT?").strip().strip('"')
        names = cat.split(",")[0::2] if cat and cat.upper() != "NO CATALOG" else []
        if MEAS in names:
            self._write(f"CALC{CH}:PAR:SEL '{MEAS}'")
            self._write(f"CALC{CH}:PAR:MOD:EXT '{sparam}'")   # VERIFY: changes the selected one
        else:
            self._write(f"CALC{CH}:PAR:DEF:EXT '{MEAS}','{sparam}'")
            self._check_errors(f"defining {MEAS} as {sparam}")
            # Show it, so the operator sees what is measured. A display
            # complaint (window missing, no free trace) is NOT fatal: the data
            # can be read without it.
            # VERIFY: `DISP:WIND1:TRAC:NEXT?` gives the next free trace number.
            try:
                n = int(float(r.query("DISP:WIND1:TRAC:NEXT?")))
                self._write(f"DISP:WIND1:TRAC{n}:FEED '{MEAS}'")
            except Exception as exc:
                self.warnings.append(f"display: {exc}")
            self.warnings.extend(self._errors())
            self._write(f"CALC{CH}:PAR:SEL '{MEAS}'")
        self._check_errors(f"selecting {sparam}")
        self._sparam = sparam

    def _errors(self) -> list[str]:
        """Drain the instrument's error queue ("+0,No error" = empty)."""
        out = []
        for _ in range(32):                        # the queue is finite; never loop forever
            e = self._res.query("SYST:ERR?").strip()
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
        if errs:
            raise RuntimeError(f"PNA reported errors while {doing}: " + " | ".join(errs))


def parse_sdata(data, points: int) -> np.ndarray:
    """SDATA (re0, im0, re1, im1, ...) -> complex array of `points` values.

    Refuses a block of the wrong length: a trace one point short, put under the
    brain's frequency grid, would shift every frequency and still look like data."""
    a = np.asarray(data, dtype=float).ravel()
    if a.size != 2 * int(points):
        raise RuntimeError(f"PNA returned {a.size} numbers for {points} points "
                           f"(expected {2 * int(points)} = re, im per point)")
    return a[0::2] + 1j * a[1::2]
