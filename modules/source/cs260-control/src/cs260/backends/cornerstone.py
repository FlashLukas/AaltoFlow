"""The real Oriel (Newport) Cornerstone 260 over GPIB, via PyVISA.

This is the ONLY file that touches `pyvisa`, and it imports it LAZILY (inside
open()), so the package imports and the simulator runs on a PC with no VISA.
On the lab PC: `uv sync --extra gui --extra real` (gotcha #29), NI-VISA +
NI-488.2 installed, then `run_service.py --real`.

Source: "Oriel Cornerstone 260 User Manual" Rev A (MKS/Newport), chapter 16
(low-level command reference) and 16.7 (error codes). The same command set is in
the Cornerstone 130 manual (M-74000). Summary of what this file relies on:

    statement            reply (GPIB, standard mode)
    UNITS?               -> "NM" | "UM" | "WN"   (we never SEND UNITS: see below)
    HANDSHAKE?           -> "0" | "1"            (HANDSHAKE 0 only if it is 1)
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

STARTING CHANGES NOTHING (Lukas's rule, 2026-09-27, for every module).
open() sends QUERIES only: it reads the units, the handshake mode, the error
state (reading STB?/ERROR? clears the error queue, like *CLS -- allowed) and
INFO?. In particular it no longer sends `UNITS NM`: the units are part of how
the user left the instrument (the hand controller shows them), so we READ them
and convert in software -- every wavelength goes over the wire in the box's
own unit and is handed to the brain in nm. The one exception left is
HANDSHAKE 0, sent ONLY if HANDSHAKE? says 1: in handshake mode every statement
answers with a status byte, which this driver does not read, so replies would
be out of step with queries. It selects the reply format, it moves nothing.

Every call not confirmed on the lab instrument is marked # VERIFY.
"""

from __future__ import annotations

import time

from ..hwlock import claim
from .base import MonoState

#: The name this module registers its claim under -- what another service is
#: told ("... already in use by cs260 (pid N)") when it tries the same address.
MODULE_KEY = "cs260"

#: Error codes of ERROR? (manual 16.7) that mean "the move you asked for will
#: not happen" -- the pending move is dropped instead of waited for forever.
_FATAL_FOR_MOVE = {1, 2, 3, 6, 8}

#: UNITS? replies we understand (manual: NM nanometres, UM micrometres,
#: WN wavenumbers in 1/cm). # VERIFY the exact reply text.
_UNITS = ("NM", "UM", "WN")


def to_nm(value: float, units: str) -> float:
    """A wavelength in the instrument's unit -> nm."""
    if units == "UM":
        return value * 1000.0
    if units == "WN":
        # 1e7 / (1/cm) = nm. Zero order (0 nm) has no finite wavenumber; the
        # box can only report it as 0 (or something huge) -- treat both as 0 nm.
        return 1e7 / value if 0.0 < value < 1e12 else 0.0
    return value


def from_nm(nm: float, units: str) -> str:
    """nm -> the text of a wavelength argument in the instrument's unit, with
    enough digits that the conversion loses nothing the drive can resolve
    (~0.01 nm per step)."""
    nm = float(nm)
    if units == "UM":
        return f"{nm / 1000.0:.6f}"
    if units == "WN":
        if nm <= 0.0:
            raise ValueError("zero order (0 nm) cannot be expressed in wavenumbers; "
                             "the instrument is set to UNITS WN")
        return f"{1e7 / nm:.4f}"
    return f"{nm:.3f}"


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
        self._lock = None                  # our claim on the GPIB address (hwlock), held while open
        self._idn = ""
        self.units = "NM"                  # what UNITS? said at open(); see to_nm()
        #: messages for the brain to show once after start (warn events)
        self.startup_notes: list[str] = []
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
        # ONE INSTRUMENT, ONE SERVICE (Lukas: "the same instrument has to be
        # defined by the same physical address"). Claim the GPIB address BEFORE
        # the first byte goes out. If another service (a second cs260, or any
        # module pointed at the same address by mistake) already drives this
        # box, HardwareBusy is raised HERE and we never talk to it -- not even
        # the harmless STB?/ERROR? queries, which would clear the other
        # service's pending error. "GPIB::4" and "GPIB0::4::INSTR" are the same
        # box: hwlock normalises the spelling. The claim is released in close()
        # and on every failure below, so a failed open never leaves it "busy".
        self._lock = claim(self._resource, MODULE_KEY)
        try:
            self._open_instrument()
        except BaseException:
            self._drop_connection()
            raise

    def _open_instrument(self) -> None:
        import pyvisa                                   # lazy: only needed for real hw
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        self._inst.write_termination = "\n"             # manual: statements end in [lf]
        self._inst.read_termination = "\n"              # replies end [cr][lf]; strip() drops the [cr]
        self._init_session()

    def _init_session(self) -> None:
        """Everything open() does once the bus is there. Split out so the
        tests can run it against a fake instrument and check it only READS."""
        self.startup_notes = []
        # Queries only from here on -- see "STARTING CHANGES NOTHING" above.
        # Reading STB? and ERROR? clears stale errors (manual 16.7), which is
        # bookkeeping, not a change of the instrument's state.
        self._query("STB?")                             # VERIFY: reading it clears stale errors
        self._query("ERROR?")
        self._adopt_handshake()
        self._adopt_units()
        self._idn = self._query("INFO?")                # VERIFY: reply format
        self._grating = self._read_grating()

    def _adopt_handshake(self) -> None:
        """Standard mode (HANDSHAKE 0, the power-up default -- VERIFY) is what
        this driver speaks. Only if the box is in handshake mode do we switch
        it: the ONE write left at start, and only when needed."""
        try:
            hs = self._query("HANDSHAKE?")              # VERIFY: query exists, "0"/"1"
        except Exception:
            hs = ""
        if hs.strip().startswith("1"):
            self._write("HANDSHAKE 0")                  # VERIFY; reply format only
            self._drain()
            self.startup_notes.append("the instrument was in HANDSHAKE 1 mode; switched "
                                      "to standard mode (HANDSHAKE 0) to be able to read it")
        elif hs.strip() != "0":
            # Not understood (older firmware?): assume the power-up default and
            # clear the "not understood" error this may have raised.
            self._query("STB?")
            self._query("ERROR?")

    def _adopt_units(self) -> None:
        """Read the instrument's wavelength unit and convert in software,
        instead of forcing it to nm (which would also change the hand
        controller's display)."""
        try:
            reply = self._query("UNITS?").upper()       # VERIFY: reply text
        except Exception:
            reply = ""
        units = next((u for u in _UNITS if reply.startswith(u)), None)
        if units is None:
            units = "NM"
            self._query("STB?")                         # clear a possible error 1
            self._query("ERROR?")
            self.startup_notes.append(f"UNITS? answered {reply!r}; assuming nm -- check "
                                      "that the wavelength readout is right")
        elif units != "NM":
            # VERIFY: how many digits WAVE? gives in UM/WN. If it were only 3
            # (0.633 um), the readout would be 1 nm coarse -- then ask Lukas
            # whether to switch the box to NM by hand, not from here.
            self.startup_notes.append(f"the instrument works in {units}; wavelengths "
                                      "are converted to nm in software (not changed on it)")
        self.units = units

    def _drain(self) -> None:
        """Throw away any reply lines still queued (status bytes of handshake
        mode), with a short timeout."""
        self._inst.timeout = 200
        try:
            for _ in range(5):
                self._inst.read()
        except Exception:
            pass
        finally:
            self._inst.timeout = self._timeout_ms

    def close(self) -> None:
        self._drop_connection()

    def _drop_connection(self) -> None:
        """Close the VISA session and give the address back. Sends NOTHING to
        the instrument (it is also the clean-up after a failed open), and the
        claim is released whatever happens on the bus."""
        try:
            if self._inst is not None:
                try:
                    self._inst.close()
                except Exception:
                    pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
        finally:
            self._inst = None
            self._rm = None
            if self._lock is not None:
                self._lock.release()
                self._lock = None

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
        # WAVE? answers in the box's own unit (UNITS?, read at open) -> nm
        wl = to_nm(float(self._query("WAVE?", long=busy)), self.units)   # VERIFY: blocks during a move?
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
        self._write(f"GOWAVE {from_nm(nm, self.units)}")  # VERIFY
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
        self._write(f"CALIBRATE {from_nm(nm, self.units)}")  # VERIFY: rewrites the stored offset
