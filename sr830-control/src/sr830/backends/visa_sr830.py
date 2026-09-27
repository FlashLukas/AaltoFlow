"""The real SR830, over GPIB, through pyvisa.

This is the only file that imports `pyvisa`, and it does so LAZILY (inside
open()), so the package imports and the simulator runs on a PC without VISA.

Source for every command below: Stanford Research Systems, "Model SR830 DSP
Lock-In Amplifier" manual, chapter 5 "Remote Programming":
  * 5-1 .. 5-3   GPIB basics: <lf> or EOI terminates a command, replies end in
                 <lf>; OUTX 1 must be sent first so replies go to GPIB;
                 the Interface Ready bit (serial poll bit 1)
  * 5-4          FMOD, FREQ, PHAS, RSLP, HARM, SLVL
  * 5-5          ISRC, IGND, ICPL, ILIN
  * 5-6 / 5-7    SENS, RMOD, OFLT, OFSL, SYNC
  * 5-9          OAUX?, AUXV
  * 5-10         OUTX, OVRM
  * 5-11         AGAN, ARSV, APHS
  * 5-15         SNAP? (X and Y at ONE instant, which OUTP? 1 then OUTP? 2
                 is not -- they would be a GPIB round trip apart)
  * 5-20 / 5-23  LIAS?, the LIA status byte bits
  * 5-24         default GPIB address 8
The command SPELLINGS are therefore from the manual; what is marked `# VERIFY`
is behaviour the manual does not pin down or that depends on the VISA / GPIB
driver on the lab PC.

Checklist for the first hardware session:
  1. NI-VISA + NI-488.2 (or Keysight IO Libraries) installed; the SR830 shows
     up in NI MAX at the address set on its front panel ([Setup] key).
  2. `uv sync --extra gui --extra real` (name BOTH extras, gotcha #29).
  3. Put the resource in the config (hardware.resource) or pass --resource.
  4. `uv run scripts/run_service.py --real`, then drive it from
     sr830_console.py and compare every value with the front panel.
"""

from __future__ import annotations

import math


class VisaSR830:
    """Drives a physical SR830. Implements the SR830Backend interface."""

    def __init__(self, resource: str = "GPIB0::8::INSTR", timeout_ms: int = 3000,
                 front_panel_override: bool = True):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._override = bool(front_panel_override)
        self._rm = None
        self._inst = None
        self._idn = ""

    # ---- low level -------------------------------------------------------------

    def _w(self, cmd: str) -> None:
        self._inst.write(cmd)

    def _q(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def _qf(self, cmd: str) -> float:
        return float(self._q(cmd))

    def _qi(self, cmd: str) -> int:
        return int(float(self._q(cmd)))      # replies like "7" -- float() tolerates "7.0"

    # ---- lifecycle ---------------------------------------------------------------

    def open(self) -> None:
        import pyvisa                                   # lazy: only for real hardware
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        # Replies end in <lf> on GPIB (manual 5-1). Commands may end in <lf> or EOI;
        # sending <lf> as well is harmless.
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        # OUTX 1 FIRST: replies go only to the selected interface, so without it
        # every query below would time out if the unit was last used over RS232.
        self._w("OUTX 1")
        # OVRM 1: keep the front panel usable while remote (5-10). # VERIFY that
        # the knobs really stay live on this unit's firmware.
        self._w(f"OVRM {1 if self._override else 0}")
        self._idn = self._q("*IDN?")
        # Clear stale latched status bits (old overloads) so the first poll
        # reports only what happens from now on.
        self._w("*CLS")
        self._q("LIAS?")

    def close(self) -> None:
        if self._inst is None:
            return
        try:
            # Hand the front panel back to the user: Go To Local.
            # VERIFY: pyvisa constant name and that the NI-488.2 driver honours it.
            try:
                from pyvisa import constants
                self._inst.control_ren(constants.RENLineOperation.address_gtl)
            except Exception:
                pass
            self._inst.close()
        finally:
            self._inst = None
            if self._rm is not None:
                try:
                    self._rm.close()
                finally:
                    self._rm = None

    def idn(self) -> str:
        return self._idn

    # ---- reference ------------------------------------------------------------------

    def set_ref_source(self, internal: bool) -> None:
        self._w(f"FMOD {1 if internal else 0}")         # 1 = internal, 0 = external

    def set_frequency(self, hz: float) -> None:
        # rounded by the unit to 5 digits or 0.1 mHz; refused in external mode
        self._w(f"FREQ {float(hz):.6g}")                 # VERIFY: 6 significant figures accepted

    def set_harmonic(self, n: int) -> None:
        self._w(f"HARM {int(n)}")

    def set_phase(self, deg: float) -> None:
        self._w(f"PHAS {float(deg):.2f}")

    def set_trigger(self, i: int) -> None:
        self._w(f"RSLP {int(i)}")

    def set_sine_out(self, volts: float) -> None:
        self._w(f"SLVL {float(volts):.3f}")

    # ---- input ---------------------------------------------------------------------------

    def set_input(self, source: int, ground: int, coupling: int, line: int) -> None:
        # VERIFY: switching ISRC between voltage and current may move the
        # sensitivity or current gain on its own (manual 5-5); the brain reads
        # the settings back after every push, so the status stays truthful.
        self._w(f"ISRC {int(source)}")
        self._w(f"IGND {int(ground)}")
        self._w(f"ICPL {int(coupling)}")
        self._w(f"ILIN {int(line)}")

    # ---- gain and filter ------------------------------------------------------------------

    def set_sensitivity(self, i: int) -> None:
        self._w(f"SENS {int(i)}")

    def set_reserve(self, i: int) -> None:
        self._w(f"RMOD {int(i)}")

    def set_time_constant(self, i: int) -> None:
        self._w(f"OFLT {int(i)}")

    def set_slope(self, i: int) -> None:
        self._w(f"OFSL {int(i)}")

    def set_sync(self, on: bool) -> None:
        self._w(f"SYNC {1 if on else 0}")

    def set_aux_out(self, k: int, volts: float) -> None:
        self._w(f"AUXV {int(k)},{float(volts):.3f}")

    # ---- read-back ---------------------------------------------------------------------------

    def read_settings(self) -> dict:
        # VERIFY: each reply is a bare number ("7", "-12.34", "0.004"); the manual
        # shows OUTP? replies that way but not every one of these.
        return {
            "sens": self._qi("SENS?"),
            "reserve": self._qi("RMOD?"),
            "tc": self._qi("OFLT?"),
            "slope": self._qi("OFSL?"),
            "phase_deg": self._qf("PHAS?"),
            "harmonic": self._qi("HARM?"),
            "sine_out_V": self._qf("SLVL?"),
        }

    # ---- data -------------------------------------------------------------------------------------

    def read_outputs(self) -> dict:
        # SNAP?1,2,9 = X, Y, reference frequency, X and Y at one instant (5-15).
        # Units: V (A in current mode). "The frequency is computed only every
        # other period or 40 ms, whichever is longer" -- fine for a readout.
        x, y, f = _floats(self._q("SNAP?1,2,9"), 3)
        return {"x": x, "y": y, "freq_Hz": f}

    def read_aux(self) -> list[float]:
        # SNAP? takes at most six parameters; the four aux inputs fit in one.
        return _floats(self._q("SNAP?5,6,7,8"), 4)

    def read_lia_status(self) -> int:
        return self._qi("LIAS?")                        # reading clears the latched bits (5-20)

    # ---- auto functions ---------------------------------------------------------------------------

    def auto(self, name: str) -> None:
        self._w({"gain": "AGAN", "reserve": "ARSV", "phase": "APHS"}[name])

    def busy(self) -> bool:
        # Serial poll bit 1 (IFC) = "no command execution in progress" (5-21).
        # A serial poll is answered even while AGAN runs; a query would sit in
        # the input buffer until it finished. # VERIFY: read_stb() on NI-488.2
        # returns the byte promptly while AGAN is running on this unit.
        stb = int(self._inst.read_stb())
        return not (stb & 0b10)


def _floats(reply: str, n: int) -> list[float]:
    parts = reply.split(",")
    if len(parts) != n:
        raise ValueError(f"expected {n} comma-separated values, got {reply!r}")
    out = [float(p) for p in parts]
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"non-finite value in {reply!r}")
    return out
