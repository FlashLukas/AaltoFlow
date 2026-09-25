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

  * SINGLE sweeps: the channel is put in HOLD on connect, and every sweep is
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

    def __init__(self, cfg: Config, resource=None, clock=time.monotonic, sleep=time.sleep):
        """`resource` = an already-open VISA resource. Tests pass a FAKE one
        (anything with write / query / query_binary_values / query_ascii_values
        / close); normally it is None and `open()` opens `hardware.visa_resource`."""
        self.cfg = cfg
        self._res = resource
        self._rm = None
        self._clock = clock
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # what the instrument is known to hold
        self._readback: dict = {}         # what it reported back for those
        self._sparam = None
        self._sweep_time = (None, math.nan)   # ((points, ifbw), seconds)
        self._correction = None
        self._pending = False
        self._points = 0
        self.warnings: list[str] = []     # non-fatal instrument complaints (display)

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        hw = self.cfg.hardware
        if self._res is None:
            try:
                import pyvisa                      # lazy: only the real backend needs it
            except ImportError as exc:
                raise RuntimeError(
                    "pyvisa is not installed. In vna-control run: "
                    "uv sync --extra gui --extra real") from exc
            self._rm = pyvisa.ResourceManager()
            self._res = self._rm.open_resource(hw.visa_resource)
        r = self._res
        r.timeout = int(float(hw.timeout_s) * 1000)       # pyvisa counts milliseconds
        r.read_termination = "\n"
        r.write_termination = "\n"
        self._applied, self._readback, self._sparam = {}, {}, None

        self._idn = r.query("*IDN?").strip()
        self._write("*CLS")                                # start with an empty error queue
        # Single sweeps on demand (see the module docstring).
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
        self._check_errors("connecting")
        self._ensure_measurement(self.cfg.sweep.sparam)

    def close(self) -> None:
        r, self._res = self._res, None
        if r is None:
            return
        try:
            if self._pending:
                r.write("ABOR")
            r.write(f"SENS{CH}:SWE:MODE CONT")             # hand the front panel back, sweeping
        except Exception:
            pass                                           # closing must not raise on a dead link
        finally:
            self._pending = False
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
        f = np.asarray(freqs_Hz, dtype=float)
        want = {"start": float(f[0]), "stop": float(f[-1]), "points": int(f.size),
                "ifbw": float(ifbw_Hz), "power": float(power_dBm)}
        changed = self._apply(want)
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
