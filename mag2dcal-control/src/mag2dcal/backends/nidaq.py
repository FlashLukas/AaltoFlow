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
  ao_readback -- a throw-away AI task in open() only: reads the two AO pins
                 through the card's internal loopback channels, so the brain
                 can ADOPT what a previous run left driving (nothing is written
                 at open()).

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
        # What the card was driving when open() found it: (x_V, y_V, enabled),
        # None where it could not be read. See read_output().
        self._found: tuple = (None, None, None)
        self.found_notes: list[str] = []   # why something could not be read

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

        # ADOPT, DON'T RESET (Lukas, 2026-09-27). This used to write DO False
        # and AO 0 V here "to be safe"; that de-energized a magnet a previous
        # run had left holding a field. Now open() only CREATES the tasks and
        # READS what the lines are doing; the brain decides from that. The
        # card keeps its last AO/DO values between programs, so what we read is
        # what the coils are really getting.
        self.found_notes = []
        x_V = y_V = enabled = None
        # The AO values first, BEFORE the main AI task exists, because the
        # read-back borrows the card's one AI timing engine for a moment.
        try:
            x_V, y_V = self._read_ao_loopback(nidaqmx)
        except Exception as exc:
            self.found_notes.append(f"AO read-back failed ({type(exc).__name__}: {exc})")
        try:
            self._do = nidaqmx.Task("mag2d_enable")
            self._do.do_channels.add_do_chan(hw.do_enable)
            # VERIFY: on an M-/X-series card a static DO task can be READ and
            # returns the state the line is driving (DAQmx keeps it from the
            # last program). VERIFY too that creating this task does not itself
            # drive the line (a tristated line after power-up reads False).
            try:
                enabled = bool(self._do.read())
            except Exception as exc:
                self.found_notes.append(f"enable line read failed "
                                        f"({type(exc).__name__}: {exc})")

            self._ao = nidaqmx.Task("mag2d_ao")
            for ch in (hw.ao_x, hw.ao_y):
                # VERIFY: adding the channels (no write, no start) leaves the
                # output at its present value -- DAQmx only changes an AO when
                # a sample is written.
                self._ao.ao_channels.add_ao_voltage_chan(ch, min_val=hw.ao_min_V,
                                                         max_val=hw.ao_max_V)

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
        self._found = (x_V, y_V, enabled)

    def _read_ao_loopback(self, nidaqmx) -> tuple[float, float]:
        """The two AO voltages, measured by the card itself.

        An AO cannot be read back as such (the 6259 has no AO "read"), but M-
        and X-series cards route every AO to an INTERNAL AI channel,
        `<Dev>/_ao0_vs_aognd`: the AI measures the output pin. One on-demand
        sample each, in a throw-away task. This is the only honest way to
        learn what a previous run left on the coils without writing anything.
        VERIFY: the internal channel names in NI MAX (Device > Channels >
        "Show internal channels"), and that the card allows them in an AI task.
        """
        hw = self.hw
        chans = []
        for phys in (hw.ao_x, hw.ao_y):
            dev, _, ch = phys.strip().partition("/")
            chans.append(f"{dev}/_{ch}_vs_aognd")
        task = nidaqmx.Task("mag2d_ao_readback")
        try:
            for ch in chans:
                task.ai_channels.add_ai_voltage_chan(ch, min_val=hw.ao_min_V,
                                                     max_val=hw.ao_max_V)
            data = task.read()                  # on demand: one sample per channel
        finally:
            task.close()
        # Undo the wiring polarity (see write_ao): the brain thinks in
        # "positive volts = positive field".
        x = float(data[0]) / (float(hw.ao_sign_x) or 1.0)
        y = float(data[1]) / (float(hw.ao_sign_y) or 1.0)
        return x, y

    def read_output(self) -> tuple:
        """(x_V, y_V, enabled) as found at open(); None where unreadable.
        Cached, because the AO read-back needs the AI engine that the main
        Hall task owns from then on."""
        return self._found

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
