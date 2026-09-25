"""The REAL backend: the vector magnet on an NI DAQ through `nidaqmx`.

This is the ONLY file that touches nidaqmx, and it imports it LAZILY inside
open(), so the package imports and the simulator runs on a PC without NI-DAQmx.
nidaqmx stays commented out in pyproject.toml until the hardware pass.

NOTHING HERE HAS RUN ON THE MAGNET YET. Every call whose details were not
confirmed is marked VERIFY. The wiring comes from the old LabVIEW VI
"RotSampleInVNA": see config.Hardware.

Tasks (one per signal kind, created in open(), closed in close()):
  ao   -- ao_x, ao_y                     on-demand writes, +-10 V
  ai   -- hall X, hall Y, temp 1, temp 2  ONE finite task, n samples at rate
  di   -- water flow switch
  do   -- output enable

WHY ONE AI TASK FOR ALL FOUR INPUTS: an M-series / X-series card has one AI
timing engine. Two AI tasks with sample clocks cannot both be reserved, so a
separate temperature task would fail (or have to be created and destroyed every
tick). read_hall() therefore acquires all four channels and CACHES the two
temperature means; read_temps() returns that cache. The brain calls read_hall()
first on every tick, so the temperatures are never more than one tick old.
"""

from __future__ import annotations

from ..config import Hardware


class NidaqVectorMagnet:
    def __init__(self, hw: Hardware):
        self.hw = hw
        self._nidaqmx = None
        self._ao = self._ai = self._di = self._do = None
        self._temps = (float("nan"), float("nan"))

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        import nidaqmx                                   # lazy: only on --real
        from nidaqmx.constants import AcquisitionType, TerminalConfiguration
        self._nidaqmx = nidaqmx
        hw = self.hw

        # VERIFY the enum names against the installed nidaqmx version
        # (DEFAULT exists in nidaqmx >= 0.6; older releases called RSE "RSE").
        terminal = {
            "default": TerminalConfiguration.DEFAULT,
            "rse": TerminalConfiguration.RSE,
            "nrse": TerminalConfiguration.NRSE,
            "diff": TerminalConfiguration.DIFF,
        }[hw.ai_terminal.strip().lower()]

        try:
            self._do = nidaqmx.Task("mag2d_enable")
            self._do.do_channels.add_do_chan(hw.do_enable)
            self._do.write(False)                        # safe first: output off

            self._ao = nidaqmx.Task("mag2d_ao")
            for ch in (hw.ao_x, hw.ao_y):
                self._ao.ao_channels.add_ao_voltage_chan(ch, min_val=hw.ao_min_V,
                                                         max_val=hw.ao_max_V)
            self._ao.write([0.0, 0.0])                   # VERIFY: list = one sample per channel

            self._ai = nidaqmx.Task("mag2d_ai")
            for ch in (hw.ai_hall_x, hw.ai_hall_y, hw.ai_temp1, hw.ai_temp2):
                self._ai.ai_channels.add_ai_voltage_chan(
                    ch, terminal_config=terminal,
                    min_val=hw.ai_min_V, max_val=hw.ai_max_V)
            n = max(2, int(hw.hall_samples))             # n >= 2 keeps read() 2-D
            self._ai.timing.cfg_samp_clk_timing(
                rate=float(hw.hall_rate_Hz), sample_mode=AcquisitionType.FINITE,
                samps_per_chan=n)

            self._di = nidaqmx.Task("mag2d_water")
            self._di.di_channels.add_di_chan(hw.di_water)
        except Exception:
            self._close_tasks()
            raise

    def close(self) -> None:
        # Backstop only: the brain has already ramped to 0 V at the slew rate.
        # If it could not (a crash), a step to 0 V is still safer than leaving
        # the coils driven.
        try:
            if self._ao is not None:
                self._ao.write([0.0, 0.0])
        except Exception:
            pass
        try:
            if self._do is not None:
                self._do.write(False)
        except Exception:
            pass
        self._close_tasks()

    def _close_tasks(self) -> None:
        for name in ("_ai", "_ao", "_di", "_do"):
            task = getattr(self, name)
            if task is not None:
                try:
                    task.close()
                except Exception:
                    pass
                setattr(self, name, None)

    # ---- Protocol --------------------------------------------------------------

    def write_ao(self, x_V: float, y_V: float) -> None:
        hw = self.hw
        # Polarity lives HERE, at the wire: the brain always thinks
        # "positive volts = positive field", and a coil wired backwards is one
        # config sign, not a change to the control loop.
        self._ao.write([float(hw.ao_sign_x) * x_V, float(hw.ao_sign_y) * y_V])

    def read_hall(self) -> tuple[float, float]:
        n = max(2, int(self.hw.hall_samples))
        # VERIFY: a finite task auto-starts on read() and returns
        # [[ch0 samples], [ch1 samples], ...] for several channels and n > 1.
        # VERIFY: the whole read must fit in timeout_s (n / rate + overhead).
        data = self._ai.read(number_of_samples_per_channel=n, timeout=self.hw.timeout_s)
        means = [sum(ch) / len(ch) for ch in data]
        self._temps = (means[2], means[3])
        return (means[0], means[1])

    def read_temps(self) -> tuple[float, float]:
        # Cached by read_hall(), see the module docstring.
        return self._temps

    def read_water(self) -> bool:
        # VERIFY: True = water flowing (the VI's convention), and no inversion
        # in the flow switch wiring.
        return bool(self._di.read())

    def set_enable(self, on: bool) -> None:
        # VERIFY: what this line actually gates (amplifier enable? a relay?) and
        # what the amplifier does if this process is KILLED: DAQmx keeps the last
        # AO and DO values. Consider a DAQmx watchdog task (expiration states
        # AO 0 V / DO False) if the card supports it.
        self._do.write(bool(on))
