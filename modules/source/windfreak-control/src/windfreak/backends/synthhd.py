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

START-UP IS READ-ONLY (2026-09-27): open() only queries; read_state()
reads f? W? h? r? E? per channel and x? *? for the reference. Every one of
those "?" forms except f? is new and marked # VERIFY.

The brain clamps every value to the configured limits BEFORE it reaches this
backend. Every line that could not be confirmed against a manual for the v2
hardware carries `# VERIFY`.
"""

from __future__ import annotations

from ..config import REFERENCE_SOURCES
from ..hwlock import claim

#: The name this module writes into the lock file, so a second service that
#: finds the COM port taken can say WHO holds it.
MODULE_KEY = "windfreak"


class SerialSynthHD:
    """Drives a physical SynthHD PRO v2. Implements the DualSynth interface."""

    def __init__(self, port: str = "COM4", timeout_s: float = 1.0,
                 pll_off_when_rf_off: bool = False,
                 phase_command: str = "relative"):
        self._port = port
        self._timeout_s = float(timeout_s)
        self._pll_off = bool(pll_off_when_rf_off)
        self._phase_relative = (str(phase_command).lower() != "absolute")
        self._ser = None
        self._idn = ""
        # Our claim on the COM port (hwlock), held from open() to close().
        # The SynthHD is identified by its COM port: that is the one physical
        # box. A second service (another windfreak, or any module pointed at
        # the same port) is refused BEFORE it sends a byte.
        self._lock = None
        # The phase command is a STEP ([API]: "These adjustments are relative
        # adjustments that add the phase amount to the current phase"). There
        # is no absolute readback, so we remember what we have added so far and
        # send the difference. The zero is therefore "the phase at open()".
        self._phase_sent = [0.0, 0.0]

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        """Connect and identify -- QUERIES ONLY.

        Rule of 2026-09-27 (Lukas): a module reads the instrument at start and
        changes nothing. So, unlike before, open() does NOT switch the outputs
        off and does NOT write the channel spacing: if the SynthHD is radiating
        when the service starts, it keeps radiating and the status says so.
        The only bytes that are not a query are the "C0"/"C1" channel selects
        in front of per-channel queries (read_state): they choose which
        channel the NEXT query addresses and change no output.
        """
        # Claim the COM port FIRST (Lukas's rule: one physical address, one
        # service). If another service holds it, HardwareBusy is raised here
        # and not a single byte has gone to the instrument.
        self._lock = claim(self._port, MODULE_KEY)
        try:
            import serial                                # lazy: pyserial, extra "real"
            self._ser = serial.Serial(port=self._port, timeout=self._timeout_s)
            self._ser.reset_input_buffer()               # drop any stale reply bytes (PC side only)
            model = self._query("+")                     # VERIFY: reply format on v2
            fw = self._query("v0")                       # VERIFY
            hw = self._query("v1")                       # VERIFY
            # The serial number ("-") is deliberately NOT part of the id string:
            # the id travels in every status frame and into data files.
            self._idn = f"Windfreak {model} (fw {fw}, hw {hw})".strip()
        except BaseException:
            # A failed open must not leave the port claimed (or open). No
            # "RF off" here: we never got as far as knowing what is on the
            # other end, and a read-only start sends no writes anyway.
            self._abandon()
            raise

    def _abandon(self) -> None:
        """Close the port WITHOUT sending anything, and give up the claim."""
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        finally:
            self._ser = None
            self._release_lock()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def read_state(self) -> dict:
        """Read both channels and the reference with queries only (see
        base.DualSynth.read_state). A query that fails is reported in
        `unread` instead of stopping the service: every one of these is still
        unconfirmed on the v2 firmware."""
        unread = []

        def q(label, cmd, conv):
            try:
                return conv(self._query(cmd))
            except Exception:
                unread.append(label)
                return None

        def flag(text):
            t = text.strip()
            if t not in ("0", "1"):
                raise ValueError(f"not a 0/1 flag: {t!r}")
            return t == "1"

        channels = []
        for ch, name in ((0, "a"), (1, "b")):
            freq = q(f"{name}.frequency_Hz", f"C{ch}f?",
                     lambda t: float(t) * 1e6)           # VERIFY: plain MHz text
            power = q(f"{name}.power_dBm", f"C{ch}W?", float)   # VERIFY: W? on v2, plain dBm
            unmuted = q(f"{name}.rf_on", f"C{ch}h?", flag)      # VERIFY: h? on v2, 1 = NOT muted
            pa = q(f"{name}.rf_on", f"C{ch}r?", flag)           # VERIFY: r? on v2
            pll = q(f"{name}.pll_on", f"C{ch}E?", flag)         # VERIFY: E? on v2
            if unmuted is None or pa is None:
                # Unknown output state: report it as possibly radiating. A
                # status that says "RF on" when it is off is an annoyance; one
                # that says "off" while the output radiates is a hazard.
                rf_on, partial = True, True
            else:
                rf_on = bool(unmuted and pa)
                partial = bool(unmuted) != bool(pa)
            channels.append({"rf_on": rf_on, "rf_partial": partial,
                             "pll_on": pll, "frequency_Hz": freq,
                             "power_dBm": power,
                             # no phase readback: the zero is "phase at open()"
                             "phase_deg": self._phase_sent[ch]})

        def ref_code(t):
            return REFERENCE_SOURCES[int(float(t))]
        ref = q("reference", "x?", ref_code)             # VERIFY: x? numbering 0 ext/1 27/2 10
        ext = q("ext_MHz", "*?", float)                  # VERIFY: *? reply = plain MHz
        return {"channels": channels, "reference": ref, "ext_MHz": ext,
                "unread": sorted(set(unread))}

    def close(self) -> None:
        try:
            if self._ser is not None:
                for ch in (0, 1):
                    self.set_output(ch, False)           # RF off on the way out
        finally:
            try:
                if self._ser is not None:
                    self._ser.close()
            finally:
                self._ser = None
                # the port is free for the next service only once it is closed
                self._release_lock()

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

    def set_channel_spacing(self, hz: float) -> None:
        # Sent only when the user changes hardware.channel_spacing_Hz, never
        # at start (it moves the frequency grid of both channels).
        self._write(f"i{float(hz):.1f}")                 # VERIFY: v2 channel spacing, units Hz ([PY])

    def read_temperature(self) -> float:
        return float(self._query("z"))                   # VERIFY: reply is plain degC text

    def idn(self) -> str:
        return self._idn
