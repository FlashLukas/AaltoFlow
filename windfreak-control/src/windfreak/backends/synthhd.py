"""The real Windfreak SynthHD PRO v2, over its USB virtual COM port.

This is the ONLY file that touches `pyserial`, and it imports it LAZILY (inside
open(), not at module top) -- so the package imports and the simulator runs on
a PC without pyserial. Install it with `uv sync --extra gui --extra real`.

Sources used (none of this has run against the instrument yet):
  [API]  Windfreak "SynthHD & HD PRO API Guide v1.0b" (hardware/firmware
         v1.4, 2016). Windfreak says v2 is ~95 % command compatible.
  [DS]   "SynthHD PRO v2 Preliminary Data Sheet v0.2d" (2019).
  [PY]   the `windfreak` PyPI package (christian-hahn/windfreak-python),
         synth_hd.py, which has a SynthHD PRO v2 model table.

The protocol, in short ([API] pages 2-5):
  * Commands are ONE character followed by the value as text, e.g. "f1000.0"
    (1000 MHz) or "W-10.000" (-10 dBm). NO terminator is sent -- the SynthHD
    rejects CR/LF -- so a command must go out as one packet, and several can
    be strung together ("C0f1000.0W0.0").
  * Queries are "<char>?" (e.g. "f?"), or the bare character for read-only
    things ("p" lock, "z" temperature, "V" leveled). Every REPLY ends in "\n".
  * Baud rate etc. are "don't cares" (USB full speed).
  * "C0"/"C1" selects which channel the following commands act on
    (RFoutA = 0, RFoutB = 1). The selection is device state that persists, so
    every per-channel call here re-sends it: one thread owns this object, but
    re-selecting costs 2 bytes and makes each call self-contained.

The commands used:
  C<n>          select channel                              [API] "Set Channel"
  f<MHz>, f?    frequency in MHz, 0.1 Hz resolution          [API] "Set Frequency"
  W<dBm>        power in dBm, levelled by the unit            [API] "Set Power"
  V             1 = leveling/calibration succeeded            [API] "Query for Successful Calibration"
  ~<deg>        phase STEP (relative!) in degrees             [API] "Set Phase Step Value"
  h<0|1>        RF mute (1 = NOT muted)                       [API] "Set RF Mute"
  r<0|1>        output amplifier power                        [API] "Set PA Power On"
  E<0|1>        PLL + VCO power ("E0r0 = full quiet")         [API] "Set PLL Power On"
  p             PLL lock status, 1 = locked                   [API] "Query for PLL Phase Lock Status"
  x<n>          reference: 0 ext, 1 int 27 MHz, 2 int 10 MHz  [API] "Set Internal or External Reference"
  *<MHz>        external reference frequency, 10..100 MHz     [API] "Set Reference Frequency"
  i<Hz>         channel spacing (v2 only)                     [PY] channelspacing
  z             temperature in degC                           [API] "Query for Internal Temperature"
  +  v0  v1     model, firmware, hardware version             [API] help list / [PY]

Cross-check with [PY] (its command table, 2026-09): the strings C, f (%.8f
MHz), W (%.3f dBm), V, ~ (%.3f, called "phase_step" there), h (called
"rf_enable" there, so 1 = RF on), r, E, p, z, x (0 external, 1 internal
27 MHz, 2 internal 10 MHz), *, i (%.1f Hz, 0.1-1000 Hz on the PRO v2), +, v0,
v1 all agree with what is used below. [PY] gives the PRO v2 power range as
-70..+20 dBm (the range the command ACCEPTS; leveling is narrower).

The brain clamps every value to the configured limits BEFORE it reaches this
backend. Every line that could not be confirmed against a manual for the v2
hardware carries `# VERIFY`.
"""

from __future__ import annotations

from ..config import REFERENCE_SOURCES


class SerialSynthHD:
    """Drives a physical SynthHD PRO v2. Implements the DualSynth interface."""

    def __init__(self, port: str = "COM4", timeout_s: float = 1.0,
                 pll_off_when_rf_off: bool = False,
                 phase_command: str = "relative",
                 channel_spacing_Hz: float = 0.0):
        self._port = port
        self._timeout_s = float(timeout_s)
        self._pll_off = bool(pll_off_when_rf_off)
        self._phase_relative = (str(phase_command).lower() != "absolute")
        self._spacing = float(channel_spacing_Hz)
        self._ser = None
        self._idn = ""
        # The phase command is a STEP ([API]: "These adjustments are relative
        # adjustments that add the phase amount to the current phase"). There
        # is no absolute readback, so we remember what we have added so far and
        # send the difference. The zero is therefore "the phase at open()".
        self._phase_sent = [0.0, 0.0]

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        import serial                                    # lazy: pyserial, extra "real"
        self._ser = serial.Serial(port=self._port, timeout=self._timeout_s)
        self._ser.reset_input_buffer()                   # drop any stale reply bytes
        # The SynthHD boots into whatever was saved in its EEPROM, which may be
        # RF ON. Make both outputs quiet FIRST, before anything else.
        for ch in (0, 1):
            self.set_output(ch, False)
        if self._spacing > 0:
            self._write(f"i{self._spacing:.1f}")         # VERIFY: v2 channel spacing, units Hz ([PY])
        model = self._query("+")                         # VERIFY: reply format on v2
        fw = self._query("v0")                           # VERIFY
        hw = self._query("v1")                           # VERIFY
        # The serial number ("-") is deliberately NOT part of the id string:
        # the id travels in every status frame and into data files.
        self._idn = f"Windfreak {model} (fw {fw}, hw {hw})".strip()

    def close(self) -> None:
        try:
            if self._ser is not None:
                for ch in (0, 1):
                    self.set_output(ch, False)           # RF off on the way out
        finally:
            if self._ser is not None:
                self._ser.close()
            self._ser = None

    # ---- the two primitives ----------------------------------------------

    def _write(self, cmd: str) -> None:
        # one packet, no terminator ([API] p.2: "does not accept termination
        # characters")
        self._ser.write(cmd.encode("ascii"))

    def _query(self, cmd: str) -> str:
        self._write(cmd)
        line = self._ser.readline()
        if not line.endswith(b"\n"):
            raise TimeoutError(f"SynthHD did not answer {cmd!r} within {self._timeout_s} s")
        return line.decode("ascii", errors="replace").strip()

    # ---- per channel -----------------------------------------------------

    def set_output(self, ch: int, on: bool) -> None:
        if on:
            # PLL on, amplifier on, unmute: "E1r1 = fully operational" [API]
            self._write(f"C{ch}E1r1h1")                  # VERIFY: h polarity on v2 (1 = not muted)
        elif self._pll_off:
            self._write(f"C{ch}h0r0E0")                  # "E0r0 = full quiet" [API]
        else:
            # keep the PLL running so it stays locked while off -> a frequency
            # set with RF off can still be verified, and switching on is instant
            self._write(f"C{ch}h0r0")                    # VERIFY: leakage with PLL on, PA off

    def set_frequency(self, ch: int, hz: float) -> None:
        self._write(f"C{ch}f{hz / 1e6:.8f}")             # MHz, [API] "fxxxxx.xxxxxxx"

    def read_frequency(self, ch: int) -> float:
        return float(self._query(f"C{ch}f?")) * 1e6      # VERIFY: reply is plain MHz text

    def set_power(self, ch: int, dBm: float) -> None:
        self._write(f"C{ch}W{dBm:.3f}")                  # [API] "Wxx.xxx"

    def set_phase(self, ch: int, deg: float) -> None:
        deg = float(deg) % 360.0
        if self._phase_relative:
            # send the STEP from where we are; 359 deg acts as -1 deg [API]
            step = (deg - self._phase_sent[ch]) % 360.0
            if step == 0.0:
                return
            self._write(f"C{ch}~{step:.3f}")             # VERIFY: still relative on v2 firmware
        else:
            self._write(f"C{ch}~{deg:.3f}")              # VERIFY: absolute variant, if v2 has it
        self._phase_sent[ch] = deg

    def read_locked(self, ch: int) -> bool:
        return self._query(f"C{ch}p").strip() == "1"     # VERIFY: per-channel after C<n>

    def read_leveled(self, ch: int) -> bool:
        return self._query(f"C{ch}V").strip() == "1"     # VERIFY: V exists on v2 firmware

    # ---- shared ----------------------------------------------------------

    def set_reference(self, source: str, ext_MHz: float) -> None:
        code = REFERENCE_SOURCES.index(source)          # 0 ext, 1 int 27, 2 int 10
        if source == "external":
            self._write(f"*{ext_MHz:.3f}")               # VERIFY: [API] "*xxx.xxx" MHz on v2
        self._write(f"x{code}")                          # VERIFY: numbering unchanged on v2

    def read_temperature(self) -> float:
        return float(self._query("z"))                   # VERIFY: reply is plain degC text

    def idn(self) -> str:
        return self._idn
