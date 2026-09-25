"""The real analyser, second make: a Copper Mountain C1209 (2 ports, 9 GHz).

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
    `*OPC?` answers "1" when it has. Only a sweep the brain triggered, after the
    magnet settled, is ever read -- the same guarantee the PNA backend gets from
    SING/HOLD. On close the trigger source goes back to INTernal, so S2VNA's
    window is left sweeping for whoever uses it next.

  * One trace (trace 1 of channel 1) shows the selected S-parameter. The old
    LabVIEW program read TWO formatted traces (amplitude and phase in degrees)
    and rebuilt the complex number from them; here the complex data comes
    directly (`CALC1:TRAC1:DATA:SDAT?`, "the corrected data array ... S-parameter
    complex values"), so nothing is lost to formatting or phase wrapping.

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

import numpy as np

from .. import model
from ..config import Config
from ..field import FieldReading
from .pna import parse_sdata

#: the channel and trace this backend owns
CH, TR = 1, 1

#: The C1209's frequency range (Copper Mountain data sheet: 100 kHz - 9 GHz).
#: `limit_envelope` pulls the config's envelope inside it.  # VERIFY on the unit
C1209_MIN_HZ = 100e3
C1209_MAX_HZ = 9e9

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


def limit_envelope(cfg: Config) -> None:
    """Pull the config's frequency envelope inside what a C1209 can do, so a
    setpoint the analyser cannot reach is clamped (and announced) by the brain
    instead of being silently moved by S2VNA."""
    lim = cfg.limits
    lim.freq_min_Hz = max(float(lim.freq_min_Hz), C1209_MIN_HZ)
    lim.freq_max_Hz = min(float(lim.freq_max_Hz), C1209_MAX_HZ)
    sw = cfg.sweep
    sw.stop_Hz = min(float(sw.stop_Hz), lim.freq_max_Hz)
    sw.start_Hz = max(min(float(sw.start_Hz), sw.stop_Hz - lim.min_span_Hz), lim.freq_min_Hz)


class CmtVna:
    simulated = False

    def __init__(self, cfg: Config, resource=None, clock=time.monotonic, sleep=time.sleep):
        """`resource` = an already-open VISA resource. Tests pass a FAKE one
        (anything with write / query / query_ascii_values / query_binary_values
        / close); normally it is None and `open()` opens `hardware.cmt_resource`."""
        self.cfg = cfg
        self._res = resource
        self._rm = None
        self._clock = clock
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # what the instrument is known to hold
        self._readback: dict = {}         # what it reported back for those
        self._sparam = None
        self._correction = None
        self._pending = False
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
            self._res = self._rm.open_resource(hw.cmt_resource)
        r = self._res
        r.timeout = int(float(hw.timeout_s) * 1000)       # pyvisa counts milliseconds
        # A raw socket has no end-of-message marker: every command MUST end in
        # a newline, and so does every reply (manual section 2.7).
        r.read_termination = "\n"
        r.write_termination = "\n"
        self._applied, self._readback, self._sparam = {}, {}, None

        self._idn = r.query("*IDN?").strip()               # "CMT,C1209,<serial>,<sw>/<hw>"
        if "C1209" not in self._idn.upper():
            self.warnings.append(f"expected a C1209, the analyser says {self._idn!r}")
        self._write("*CLS")                                # start with an empty error queue
        # one trace on channel 1, active, showing what we measure
        self._write(f":CALC{CH}:PAR:COUN 1")
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
        self._check_errors("connecting")
        self._ensure_sparam(self.cfg.sweep.sparam)

    def close(self) -> None:
        r, self._res = self._res, None
        if r is None:
            return
        try:
            if self._pending:
                r.write(":ABOR")
            # Hand S2VNA back sweeping on its own, like a front panel.
            r.write(":TRIG:SOUR INT")
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

    # ---- sweeping ----------------------------------------------------------------

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        """The rule of thumb (~1.2 / IFBW per point). S2VNA has no sweep-time
        query in the manual, so this is only the brain's first wait; the real
        end of the sweep is `*OPC?`. Never queries (status() calls it)."""
        return model.sweep_time_s(points, ifbw_Hz)

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None) -> None:
        if self._res is None:
            raise RuntimeError("C1209 is not connected")
        f = np.asarray(freqs_Hz, dtype=float)
        want = {"start": float(f[0]), "stop": float(f[-1]), "points": int(f.size),
                "ifbw": float(ifbw_Hz), "power": float(power_dBm)}
        changed = self._apply(want)
        if sparam != self._sparam:
            self._ensure_sparam(sparam)
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
            raise TimeoutError(f"C1209 sweep did not finish within {ceiling:.0f} s ({exc})")
        finally:
            r.timeout = old
        self._pending = False
        if not done.endswith("1"):
            raise RuntimeError(f"C1209: unexpected *OPC? reply {done!r}")

        query = f":CALC{CH}:TRAC{TR}:DATA:SDAT?"
        if self._binary():
            data = r.query_binary_values(query, datatype="d", is_big_endian=False,
                                         container=np.array)
        else:
            data = r.query_ascii_values(query, container=np.array)
        z = parse_sdata(data, self._points)
        meta = {"ifbw_actual_Hz": self._readback.get("ifbw", math.nan),
                "power_actual_dBm": self._readback.get("power", math.nan),
                "correction_on": self._correction,
                "sweep_s": self._clock() - self._started_at,
                "f_res_model_Hz": math.nan}
        return z, meta

    def abort_sweep(self) -> None:
        if self._pending and self._res is not None:
            self._pending = False
            # With continuous initiation on, ABOR puts the channel back into
            # "waiting for trigger" (manual: ABOR), ready for the next TRIG:SING.
            # VERIFY: ABOR is acted on while a TRIG:SING is still pending.
            self._write(":ABOR")
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
            raise RuntimeError("C1209 did not take the sweep: " + "; ".join(bad))

    def _ensure_sparam(self, sparam: str) -> None:
        if sparam not in model.SPARAMS:
            raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sparam!r}")
        self._write(f":CALC{CH}:PAR{TR}:DEF {sparam}")
        self._write(f":CALC{CH}:PAR{TR}:SEL")
        self._check_errors(f"selecting {sparam}")
        self._sparam = sparam

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
        if errs:
            raise RuntimeError(f"C1209 reported errors while {doing}: " + " | ".join(errs))
