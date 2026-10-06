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

ONE INSTRUMENT, ONE SERVICE (Lukas: "the same instrument has to be defined by
the same physical address"): mag2d and mag2dcal drive the SAME coils through
the SAME DAQ card. The physical address of that box is the DAQmx DEVICE NAME
("Dev1" -- the part of every channel string before the "/"). open() claims
every device the configured channels name (normally just Dev1) through
hwlock.py BEFORE the first task is created, so a second service -- this one
started twice, or mag2dcal -- is refused instead of fighting over the coils.
The claims are released in close() and on every failed open().
"""

from __future__ import annotations

from .. import hwlock
from ..config import Hardware

# The name this module claims addresses under; the refusal message of the
# OTHER service names it ("DEV1 is already in use by mag2d (pid ...)").
MODULE = "mag2d"


def daq_devices(hw: Hardware) -> list[str]:
    """The DAQmx device names (physical boxes) the configured channels use.

    "Dev1/ao0" -> "Dev1"; "/Dev1/port0/line1" -> "Dev1"; a comma list
    "Dev1/ai0, Dev2/ai1" names two cards. Sorted and de-duplicated
    case-insensitively (DAQmx itself ignores case: dev1 IS Dev1), so the claim
    order is fixed -- two processes claiming two cards in different orders
    could otherwise each get one and block the other.
    """
    found: dict[str, str] = {}
    for spec in (hw.ao_x, hw.ao_y, hw.ai_hall_x, hw.ai_hall_y, hw.ai_temp1,
                 hw.ai_temp2, hw.di_water, hw.do_enable):
        for part in str(spec).split(","):
            dev = part.strip().lstrip("/").partition("/")[0].strip()
            if dev:
                found.setdefault(hwlock.normalize(dev), dev)
    return [found[k] for k in sorted(found)]


class NidaqVectorMagnet:
    def __init__(self, hw: Hardware):
        self.hw = hw
        self._nidaqmx = None
        self._ao = self._ai = self._di = self._do = None
        self._temps = (float("nan"), float("nan"))
        self._ao_readback: tuple[float, float] | None = None
        self._locks: list[hwlock.HardwareLock] = []

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

        # Claim the card(s) BEFORE the first DAQmx task exists: creating a task
        # already reserves channels on the card, and the other service may be in
        # the middle of a ramp. HardwareBusy propagates to the service, which
        # prints one line and exits WITHOUT closing anything (close() of a card
        # we never claimed would write 0 V onto somebody else's magnet).
        self._claim_devices()
        try:
            # ADOPT ON START (Lukas, 2026-09-27): NOTHING is written here. The
            # old code forced enable False and AO 0 V -- a magnet left energized
            # by a crashed run would have dropped to zero field in one step.
            # Now the brain reads the state (read_output_state) and takes over
            # from it. The first write happens only when the control loop or a
            # command actually wants a different value.
            #
            # VERIFY: creating a DO task on an output line does not change the
            # line's level before the first write (on M/X-series a line keeps
            # its last driven state, but a line that was never an output since
            # power-up may float/pull until the task commits).
            self._do = nidaqmx.Task("mag2d_enable")
            self._do.do_channels.add_do_chan(hw.do_enable)

            # VERIFY: creating (not writing) an AO task leaves the output at its
            # last value -- DAQmx holds AO after a task closes or a process exits.
            self._ao = nidaqmx.Task("mag2d_ao")
            for ch in (hw.ao_x, hw.ao_y):
                self._ao.ao_channels.add_ao_voltage_chan(ch, min_val=hw.ao_min_V,
                                                         max_val=hw.ao_max_V)

            # Read the present drive BEFORE the main AI task reserves the AI
            # timing engine (one per card, see the module docstring).
            self._ao_readback = self._read_ao_internal(nidaqmx)

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
        except BaseException:
            # A failed open must not leave the card claimed: the next start
            # (or mag2dcal) would be refused for a box nobody is driving.
            self._close_tasks()
            self._release_devices()
            raise

    def _claim_devices(self) -> None:
        """Claim every DAQ device, all or nothing."""
        try:
            for dev in daq_devices(self.hw):
                self._locks.append(hwlock.claim(dev, MODULE))
        except BaseException:
            self._release_devices()          # e.g. got Dev1, Dev2 was busy
            raise

    def _release_devices(self) -> None:
        locks, self._locks = self._locks, []
        for lock in locks:
            lock.release()

    def _read_ao_internal(self, nidaqmx) -> tuple[float, float] | None:
        """Measure what the two AO channels are putting out now, or None.

        An NI card cannot read an AO value back directly (clMag's 6259 note),
        but M- and X-series cards route each AO to an INTERNAL AI channel,
        "<dev>/_ao0_vs_aognd". One on-demand read of those is a pure
        measurement: nothing on the output changes. If the card has no such
        channel the brain is told None and estimates the drive from the field.

        VERIFY: the internal channel names on the lab card (NI MAX > Device >
        "Show internal channels"), and that an on-demand read needs no timing.
        """
        hw = self.hw
        try:
            names = []
            for ch in (hw.ao_x, hw.ao_y):
                dev, _, line = ch.strip().partition("/")
                names.append(f"{dev}/_{line}_vs_aognd")
            task = nidaqmx.Task("mag2d_ao_readback")
            try:
                for name in names:
                    task.ai_channels.add_ai_voltage_chan(
                        name, min_val=hw.ao_min_V, max_val=hw.ao_max_V)
                vx, vy = task.read()                     # VERIFY: one value per channel
            finally:
                task.close()
            # undo the wiring polarity: the brain thinks "+V = +B"
            return (float(vx) * float(hw.ao_sign_x), float(vy) * float(hw.ao_sign_y))
        except Exception:
            return None

    def read_output_state(self) -> tuple[bool | None, tuple[float, float] | None]:
        # VERIFY: reading a DO task returns the level the line is DRIVEN at
        # (supported for port0 lines on M/X-series); None if the card refuses.
        try:
            enable = bool(self._do.read())
        except Exception:
            enable = None
        return enable, self._ao_readback

    def close(self, output_off: bool = True) -> None:
        # (Shutdown behaviour, unchanged by the adopt-on-start rule.)
        # Backstop only: the brain has already ramped to 0 V at the slew rate.
        # If it could not (a crash), a step to 0 V is still safer than leaving
        # the coils driven.
        # output_off=False is a RESTART (shutdown{keep_outputs: true}): no
        # backstop write, the coils keep their drive.
        # VERIFY: closing an AO/DO task leaves the card driving its last value
        # (the usual M/X-series behaviour) -- if this card resets on task
        # close, a restart drops the field anyway.
        if output_off:
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
        # Release the card only AFTER the backstop writes and the task closes:
        # the moment it is free, another service may open it.
        self._release_devices()

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
