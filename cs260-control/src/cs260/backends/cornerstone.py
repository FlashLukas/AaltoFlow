"""The real Oriel (Newport) Cornerstone 260 over GPIB, via PyVISA.

This is the ONLY file that touches `pyvisa`, and it imports it LAZILY (inside
open()), so the package imports and the simulator runs on a PC with no VISA.
On the lab PC: `uv sync --extra gui --extra real` (gotcha #29), NI-VISA +
NI-488.2 installed, then `run_service.py --real`.

Source: "Oriel Cornerstone 260 User Manual" Rev A (MKS/Newport), chapter 16
(low-level command reference) and 16.7 (error codes). The same command set is in
the Cornerstone 130 manual (M-74000). Summary of what this file relies on:

    statement            reply (GPIB, standard mode)
    UNITS NM             none
    GOWAVE <nm>          none            WAVE?          -> "500.000"
    GRAT <n>             none            GRAT?          -> "1,1200,BLUE"
    GRAT<n>LINES?        -> "1200"       GRAT<n>LABEL?  -> "BLUE"
    SHUTTER O | C        none            SHUTTER?       -> "O" | "C"
    FILTER <n>           none            FILTER?        -> "3"; "0" = no wheel OR wheel
                                                          out of position (i.e. moving),
                                                          AND it sets an error (16.6)
    OUTPORT <n>          none            OUTPORT?       -> "1" axial | "2" lateral
    STEP <n>             none            STEP?          -> steps from home
    ABORT                none
    CALIBRATE <nm>       none
    INFO?                -> "Oriel,Model 74100 Cornerstone 260,SN...,V..."
    STB?                 -> "00" ok | non-zero = error; reading clears it
                            (16.6 says "32", 16.7 says "20" for the same
                            bit 5 -- decimal vs hex -- so ANY non-zero counts)
    ERROR?               -> 0..9 (1 not understood, 2 bad parameter,
                            3 destination not allowed, 6 accessory absent,
                            7 already there, 8 could not home, 9 label too long)
    HANDSHAKE 0          standard mode: no reply to commands

Under GPIB there is NO echo (the echo is an RS-232 thing), statements end in
[lf], replies in [cr][lf]. The factory GPIB address is 4. The instrument picks
RS-232 or GPIB from the FIRST statement after power-up, and ignores the PC
entirely while the hand controller is active (press LOCAL on it).

HOW "ARRIVED" IS DECIDED -- the part to check first on the real unit:
the manual documents no "busy" bit (STB? only flags errors). What it does say
is that the instrument processes statements one at a time and, in handshake
mode, reports the status byte "upon completion of the Statement". The RS-232
Python driver by B. Carlsen reads WAVE? right after GOWAVE and gets the final
position, i.e. a query is answered only once the move has finished. We do not
rely on either alone: after any move is started this backend keeps `moving`
True until the instrument confirms the END STATE (WAVE? within arrive_tol of
the target twice in a row; GRAT?/FILTER?/OUTPORT? equal to the target; for a
grating swap WAVE? stable twice after a minimum time), and it gives reads made
during a move the long `move_timeout_ms`, so a query that blocks until the move
is over simply comes back late instead of timing out.

Every call not confirmed on the lab instrument is marked # VERIFY.
"""

from __future__ import annotations

import time

from .base import MonoState

#: Error codes of ERROR? (manual 16.7) that mean "the move you asked for will
#: not happen" -- the pending move is dropped instead of waited for forever.
_FATAL_FOR_MOVE = {1, 2, 3, 6, 8}


class CornerstoneGPIB:
    """Drives a physical Cornerstone 260. Implements MonochromatorBackend."""

    simulated = False

    def __init__(self, resource: str = "GPIB0::4::INSTR", timeout_ms: int = 3000,
                 move_timeout_ms: int = 30000, arrive_tol_nm: float = 0.5,
                 n_gratings: int = 2, filter_wheel: bool = False,
                 dual_port: bool = False, clock=time.monotonic):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._move_timeout_ms = int(move_timeout_ms)
        self._tol = float(arrive_tol_nm)
        self._n_gratings = int(n_gratings)
        self._filter_wheel = bool(filter_wheel)
        self._dual_port = bool(dual_port)
        self._clock = clock
        self._rm = None
        self._inst = None
        self._idn = ""
        # the move we started and are waiting to see finished:
        # (kind, target, t_started) -- kind in wave/step/grating/filter/port
        self._pending = None
        self._last_wl = None
        self._same = 0                     # consecutive near-identical WAVE? replies
        # last known values, so a poll that skips a query still reports them
        self._grating = 1
        self._filter = 0
        self._port = 1

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        import pyvisa                                   # lazy: only needed for real hw
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        self._inst.write_termination = "\n"             # manual: statements end in [lf]
        self._inst.read_termination = "\n"              # replies end [cr][lf]; strip() drops the [cr]
        self._write("HANDSHAKE 0")                      # VERIFY: standard mode (no reply to commands)
        self._write("UNITS NM")                         # VERIFY: all wavelengths in nm from now on
        self._query("STB?")                             # VERIFY: reading it clears stale errors
        self._query("ERROR?")                           # manual 16.7: reading STB? AND ERROR? resets both
        self._idn = self._query("INFO?")                # VERIFY: reply format
        self._grating = self._read_grating()

    def close(self) -> None:
        try:
            if self._inst is not None:
                self._inst.close()
        finally:
            if self._rm is not None:
                self._rm.close()
            self._inst = None
            self._rm = None

    def idn(self) -> str:
        return self._idn

    def configure_accessories(self, filter_wheel: bool, dual_port: bool) -> None:
        """Called by the brain after a settings change: which accessories to
        poll. Only fitted ones are queried, because FILTER? on an absent wheel
        answers 0 AND sets an instrument error (manual 16.6)."""
        self._filter_wheel = bool(filter_wheel)
        self._dual_port = bool(dual_port)

    # ---- small helpers ---------------------------------------------------------

    def _write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def _query(self, cmd: str, long: bool = False) -> str:
        """One query. `long` = a move may be in progress, so allow it to take
        as long as the move (see the module docstring)."""
        self._inst.timeout = self._move_timeout_ms if long else self._timeout_ms
        try:
            return self._inst.query(cmd).strip()
        finally:
            self._inst.timeout = self._timeout_ms

    def _read_grating(self) -> int:
        # "1,1200,BLUE" -> 1
        return int(self._query("GRAT?", long=self._pending is not None).split(",")[0])  # VERIFY

    def grating_info(self, n: int) -> tuple[int, str] | None:
        try:
            lines = int(float(self._query(f"GRAT{int(n)}LINES?")))   # VERIFY
            label = self._query(f"GRAT{int(n)}LABEL?")               # VERIFY
            return lines, label
        except Exception:
            return None

    # ---- state -----------------------------------------------------------------

    def read_state(self) -> MonoState:
        busy = self._pending is not None
        kind_now = self._pending[0] if busy else ""
        wl = float(self._query("WAVE?", long=busy))                      # VERIFY: blocks during a move?
        self._grating = self._read_grating()
        shutter = self._query("SHUTTER?").upper().startswith("O")        # VERIFY: "O"/"C"
        filter_in_transit = False
        # A pending filter/port move is always polled, even if the flag says
        # "not fitted" -- otherwise the move could never be seen to finish.
        if self._filter_wheel or kind_now == "filter":
            self._filter = int(float(self._query("FILTER?", long=busy)))  # VERIFY
            # "0" while OUR filter move runs = the wheel is between positions
            # (manual 16.6); the error it raises alongside is not a failure.
            filter_in_transit = self._filter == 0 and kind_now == "filter"
        if self._dual_port or kind_now == "port":
            self._port = int(float(self._query("OUTPORT?", long=busy)))   # VERIFY
        try:
            step = int(float(self._query("STEP?")))                      # VERIFY
        except ValueError:
            step = 0
        error = None
        stb = self._query("STB?")                                        # VERIFY: "00" / "32" or "20"
        # Any non-zero status byte is an error (manual 16.7: "00 for success or
        # non-zero for error"). Testing bit 5 (& 32) would MISS it if the box
        # answers "20" -- which is bit 5 written in hex, as 16.7 also shows.
        try:
            stb_bad = int(float(stb or 0)) != 0
        except ValueError:
            stb_bad = bool(stb.strip("0 "))
        if stb_bad:
            try:
                error = int(float(self._query("ERROR?")))                # VERIFY: 0..9
            except ValueError:
                error = 0
            if filter_in_transit and error == 6:
                error = None                                             # see above

        # --- decide whether the pending move is over -------------------------
        if self._last_wl is not None and abs(wl - self._last_wl) < 1e-3:
            self._same += 1
        else:
            self._same = 0
        self._last_wl = wl
        if self._pending is not None:
            kind, target, t0 = self._pending
            if error in _FATAL_FOR_MOVE:
                done = True                       # it will not happen: stop waiting
            elif kind == "wave":
                done = abs(wl - target) <= self._tol and self._same >= 1
            elif kind == "step":
                done = self._same >= 1 and self._clock() - t0 > 0.3
            elif kind == "grating":
                done = (self._grating == target and self._same >= 1
                        and self._clock() - t0 > 1.0)
            elif kind == "filter":
                done = self._filter == target
            elif kind == "port":
                done = self._port == target
            else:
                done = True
            if done:
                self._pending = None
        return MonoState(wavelength_nm=wl, grating=self._grating, shutter_open=shutter,
                         filter=self._filter, port=self._port, step_position=step,
                         moving=self._pending is not None, error_code=error)

    # ---- moves -------------------------------------------------------------------

    def _begin(self, kind: str, target) -> None:
        self._pending = (kind, target, self._clock())
        self._same = 0

    def goto(self, nm: float) -> None:
        self._write(f"GOWAVE {float(nm):.3f}")          # VERIFY
        self._begin("wave", float(nm))

    def set_grating(self, n: int) -> None:
        self._write(f"GRAT {int(n)}")                   # VERIFY
        self._begin("grating", int(n))

    def set_filter(self, n: int) -> None:
        self._write(f"FILTER {int(n)}")                 # VERIFY
        self._begin("filter", int(n))

    def set_port(self, n: int) -> None:
        self._write(f"OUTPORT {int(n)}")                # VERIFY
        self._begin("port", int(n))

    def step(self, n: int) -> None:
        self._write(f"STEP {int(n)}")                   # VERIFY: sign = direction?
        self._begin("step", int(n))

    # ---- instantaneous -------------------------------------------------------------

    def set_shutter(self, open_: bool) -> None:
        self._write("SHUTTER O" if open_ else "SHUTTER C")   # VERIFY

    def abort(self) -> None:
        # VERIFY: is ABORT acted on while a GOWAVE is executing, or queued
        # behind it (statements are handled one at a time)?
        self._write("ABORT")
        self._pending = None

    def calibrate(self, nm: float) -> None:
        self._write(f"CALIBRATE {float(nm):.3f}")       # VERIFY: rewrites the stored offset
