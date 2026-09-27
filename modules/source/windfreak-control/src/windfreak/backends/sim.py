"""Simulated hardware: a fake Windfreak SynthHD PRO v2.

It implements the DualSynth interface from `base`, so the brain cannot tell it
apart from the real instrument. The behaviour is modelled on the datasheet
(SynthHD PRO v2 Preliminary Data Sheet v0.2d) and the API guide, so that the
things that go wrong on the real box also go wrong here:

  * FREQUENCY GRID -- the PLL can only make multiples of its channel spacing
    (factory default 100 Hz). Ask for 1 GHz + 30 Hz and you get 1 GHz; the
    readback shows the snapped value.
  * LOCK -- after a frequency hop the PLL needs ~100 us (datasheet "100 uS RF
    lock time"); after powering the PLL up ~20 ms (API guide, command E). With
    "external" reference selected and nothing on REF IN, or a reference whose
    frequency does not match what was declared, it never locks.
  * LEVELING -- the output can only be leveled between about -40 dBm and a
    maximum that falls with frequency (+20 dBm at low frequency, ~+15 at
    12 GHz, ~+6 at 24 GHz: datasheet section 3.1). Outside that, or above
    20 GHz (uncalibrated), the "leveled" flag (API command V) reads 0.
  * TEMPERATURE -- the internal sensor warms towards ~30 C plus ~9 C per
    powered output amplifier (420 mA per channel), with a two-minute time
    constant.
"""

from __future__ import annotations

import math
import time

from ..config import REFERENCE_SOURCES

# (frequency GHz, maximum leveled power dBm), linear in between. Read off the
# datasheet's typical max-gain curve and its note "+15 dBm setting hitting max
# power limit above 12 GHz"; the sim does not need more precision than that.
_PMAX_CURVE = ((0.0, 20.0), (6.0, 20.0), (12.0, 15.0), (20.0, 10.0), (24.0, 6.0))
_PMIN_LEVELED = -40.0          # datasheet: "RF Output Power Minimum ... -40 dBm"
_CAL_MAX_HZ = 20e9             # datasheet: "Calibration is good from 10MHz to 20GHz"
_HOP_LOCK_S = 100e-6           # datasheet: "100uS RF lock time standard"
_PLL_BOOT_S = 0.020            # API guide, command E: "can take 20mS to boot up"


def max_leveled_power(hz: float) -> float:
    """The highest power the simulated channel can level at `hz`."""
    g = hz / 1e9
    pts = _PMAX_CURVE
    if g <= pts[0][0]:
        return pts[0][1]
    for (g0, p0), (g1, p1) in zip(pts, pts[1:]):
        if g <= g1:
            return p0 + (p1 - p0) * (g - g0) / (g1 - g0)
    return pts[-1][1]


class _SimChannel:
    def __init__(self, frequency_Hz=1e9, power_dBm=-10.0, phase_deg=0.0, rf_on=False,
                 pll_on=True):
        self.freq_Hz = float(frequency_Hz)
        self.power_dBm = float(power_dBm)
        self.phase_deg = float(phase_deg)
        self.pa_on = bool(rf_on)          # output amplifier ("r")
        self.unmuted = bool(rf_on)        # RF mute ("h", 1 = not muted)
        self.pll_on = bool(pll_on)        # PLL + VCO ("E")
        self.lock_after = 0.0             # monotonic time from which the PLL is locked


#: What the pretend SynthHD is doing BEFORE the service starts -- as if someone
#: had left it running from the Windfreak GUI, or its EEPROM powers it up like
#: this. Deliberately NOT the config defaults (1 GHz, -10 dBm, RF off,
#: internal 27 MHz): the service must READ this state and adopt it, and a test
#: can only tell adoption from "pushed the defaults" if the two differ.
SIM_BOOT_STATE = {
    "channels": [
        {"frequency_Hz": 2.45e9, "power_dBm": -5.0, "phase_deg": 0.0, "rf_on": True},
        {"frequency_Hz": 3.2e9, "power_dBm": -12.0, "phase_deg": 90.0, "rf_on": False},
    ],
    "reference": "internal_10MHz",
    "ext_MHz": 10.0,
}


class SimulatedSynthHD:
    """Pretends to be a Windfreak SynthHD PRO v2 (two channels).

    `external_ref_MHz` is what is plugged into REF IN in this pretend lab:
    None = nothing. Tests set it to see an external reference lock.
    """

    def __init__(self, channel_spacing_Hz: float = 100.0,
                 pll_off_when_rf_off: bool = False,
                 external_ref_MHz: float | None = None,
                 boot: dict | None = None):
        self.spacing = float(channel_spacing_Hz) if channel_spacing_Hz > 0 else 100.0
        self.pll_off_when_rf_off = bool(pll_off_when_rf_off)
        self.external_ref_MHz = external_ref_MHz
        boot = SIM_BOOT_STATE if boot is None else boot
        self.ch = [_SimChannel(**c) for c in boot["channels"]]
        self.ref_source = boot.get("reference", "internal_27MHz")
        self.ref_ext_MHz = float(boot.get("ext_MHz", 10.0))
        # every command that CHANGES the instrument, in order -- so a test can
        # prove that start-up sends none (the read-only start rule)
        self.writes: list[tuple] = []
        self._open = False
        self._temp_C = 28.0
        self._temp_t = time.monotonic()

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        # Connect and change nothing (read-only start rule, 2026-09-27): the
        # pretend box keeps whatever it was doing, RF included.
        self._open = True

    def read_state(self) -> dict:
        chans = []
        for c in self.ch:
            chans.append({"rf_on": c.pa_on and c.unmuted,
                          "rf_partial": c.pa_on != c.unmuted,
                          "pll_on": c.pll_on,
                          "frequency_Hz": c.freq_Hz,
                          "power_dBm": c.power_dBm,
                          # the sim CAN read its phase (its coordinate is
                          # absolute); the real backend reports its own zero
                          "phase_deg": c.phase_deg})
        return {"channels": chans, "reference": self.ref_source,
                "ext_MHz": self.ref_ext_MHz, "unread": []}

    def close(self) -> None:
        # shutdown is not part of the read-only rule: RF off on the way out
        for i in (0, 1):
            self.set_output(i, False)
        self._open = False

    # ---- per channel -----------------------------------------------------

    def set_output(self, ch: int, on: bool) -> None:
        self.writes.append(("set_output", ch, bool(on)))
        c = self.ch[ch]
        now = time.monotonic()
        if on:
            if not c.pll_on:
                c.pll_on = True
                c.lock_after = now + _PLL_BOOT_S
            c.pa_on = c.unmuted = True
        else:
            c.pa_on = c.unmuted = False
            if self.pll_off_when_rf_off:
                c.pll_on = False

    def output_on(self, ch: int) -> bool:
        c = self.ch[ch]
        return c.pa_on and c.unmuted

    def set_frequency(self, ch: int, hz: float) -> None:
        self.writes.append(("set_frequency", ch, float(hz)))
        c = self.ch[ch]
        # the PLL can only make multiples of its channel spacing
        c.freq_Hz = round(float(hz) / self.spacing) * self.spacing
        c.lock_after = max(c.lock_after, time.monotonic() + _HOP_LOCK_S)

    def read_frequency(self, ch: int) -> float:
        return self.ch[ch].freq_Hz

    def set_power(self, ch: int, dBm: float) -> None:
        self.writes.append(("set_power", ch, float(dBm)))
        self.ch[ch].power_dBm = float(dBm)

    def set_phase(self, ch: int, deg: float) -> None:
        self.writes.append(("set_phase", ch, float(deg)))
        self.ch[ch].phase_deg = float(deg) % 360.0

    def read_locked(self, ch: int) -> bool:
        c = self.ch[ch]
        return c.pll_on and self._reference_ok() and time.monotonic() >= c.lock_after

    def read_leveled(self, ch: int) -> bool:
        c = self.ch[ch]
        if not self.read_locked(ch) or c.freq_Hz > _CAL_MAX_HZ:
            return False
        return _PMIN_LEVELED <= c.power_dBm <= max_leveled_power(c.freq_Hz)

    def output_power_dBm(self, ch: int) -> float:
        """What a power meter on the connector would read (sim only)."""
        c = self.ch[ch]
        if not self.output_on(ch) or not self.read_locked(ch):
            return -90.0                    # datasheet "RF OFF Output Power"
        return min(max(c.power_dBm, _PMIN_LEVELED), max_leveled_power(c.freq_Hz))

    # ---- shared ----------------------------------------------------------

    def set_reference(self, source: str, ext_MHz: float) -> None:
        if source not in REFERENCE_SOURCES:
            raise ValueError(f"unknown reference {source!r}")
        self.writes.append(("set_reference", source, float(ext_MHz)))
        now = time.monotonic()
        self.ref_source = source
        self.ref_ext_MHz = float(ext_MHz)
        # a reference change makes both PLLs re-acquire
        for c in self.ch:
            c.lock_after = max(c.lock_after, now + _HOP_LOCK_S)

    def _reference_ok(self) -> bool:
        if self.ref_source != "external":
            return True
        if self.external_ref_MHz is None:
            return False                    # nothing on REF IN
        # a mismatch pulls the PFD out of range: no lock (0.1 % window)
        return abs(self.external_ref_MHz - self.ref_ext_MHz) <= 1e-3 * self.ref_ext_MHz

    def set_channel_spacing(self, hz: float) -> None:
        self.writes.append(("set_channel_spacing", float(hz)))
        self.spacing = float(hz) if hz > 0 else self.spacing

    def read_temperature(self) -> float:
        # first-order approach towards the steady state for the load now on
        now = time.monotonic()
        dt, self._temp_t = now - self._temp_t, now
        target = 30.0 + 9.0 * sum(c.pa_on for c in self.ch)
        self._temp_C += (target - self._temp_C) * (1.0 - math.exp(-dt / 120.0))
        return self._temp_C

    def idn(self) -> str:
        return "Windfreak SynthHD PRO v2 (SIMULATED)" if self._open else ""
