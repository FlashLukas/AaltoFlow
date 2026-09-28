"""The REAL backend: an NI USB-6001 through `nidaqmx`.

This is the ONLY file that touches nidaqmx, and it imports it LAZILY inside
open(), so the package imports and the simulator runs on a PC without NI-DAQmx.
nidaqmx is in the optional extra `real`:
    uv sync --extra gui --extra real       (both, or `uv sync` removes one: gotcha #29)

NOTHING HERE HAS RUN ON A USB-6001 YET. Every call whose details were not
confirmed on the card is marked VERIFY. The patterns follow the suite's other
DAQmx code (mag2d-control's backends/nidaq.py, clMag's AUX I/O on a 6259).

TASKS (created once in open(), from the LAYOUT fixed at service start):
  ai      every enabled input in ONE finite task: the card has one converter
          and one AI sample clock, so a second timed AI task could not run.
  ao<n>   ONE TASK PER AO CHANNEL, on-demand writes, never written at start.
          Why not one task for both: a write to a two-channel task sets BOTH
          outputs, so changing ao0 would force a value onto ao1 -- whose level
          we do not know after a restart (the 6001 cannot read AO back).
          VERIFY that the 6001 accepts two static AO tasks at once.
  di      every input line in one task, one channel per line.
  do_<n>  ONE TASK PER OUTPUT LINE. Why not one task for all outputs: writing
          a multi-line task writes EVERY line in it, and at start we do not know
          the level of a line we have not driven yet (adopt-on-start says: do
          not touch it). One task per line lets set_do change exactly one line.
          VERIFY that the 6001 accepts several static DO tasks on one port.

ONE CARD, ONE SERVICE (hwlock, docs/DEVELOPER_NOTES.md gotcha #37). The card is
claimed twice, all or nothing, BEFORE the first task exists:
  * by its SERIAL number ("NI-DAQ-SN:1A2B3C4") -- the physical box, so two
    services pointed at it under different NI MAX aliases still collide;
  * by its DAQmx device NAME ("Dev1") -- because the older DAQ modules (clMag,
    mag2d) claim cards by name, and a USB-6001 that one of them was pointed at
    must be refused here too. A name names exactly one box on this PC at a time,
    so this claim can never refuse a different card.
Both are released in close() and on every failed open().
"""

from __future__ import annotations

from .. import hwlock
from ..config import Hardware, daqmx_line
from .base import Layout

#: The name this module claims addresses under; another service's refusal
#: message names it ("DEV1 is already in use by usb6001 (pid ...)").
MODULE = "usb6001"


def serial_address(serial) -> str:
    """The hwlock address of a card with this serial number (int or text)."""
    if isinstance(serial, int):
        serial = f"{serial:X}"          # NI MAX shows the serial in hex
    return f"NI-DAQ-SN:{str(serial).strip().upper()}"


class NidaqUsb6001:
    def __init__(self, hw: Hardware):
        self.hw = hw
        self._nidaqmx = None
        self._ai = self._di = None
        self._ao: dict[int, object] = {}
        self._do: dict[int, object] = {}
        self._locks: list[hwlock.HardwareLock] = []
        self._timing = None               # (samples, rate) the AI task is set up for
        self._idn = ""
        self.layout = Layout()

    # ---- lifecycle --------------------------------------------------------------

    def open(self, layout: Layout) -> None:
        import nidaqmx                                   # lazy: only on --real
        import nidaqmx.system                            # VERIFY: needed for .system.Device
        from nidaqmx.constants import LineGrouping, TerminalConfiguration
        self._nidaqmx = nidaqmx
        self.layout = layout
        dev_name = self.hw.device.strip()

        # 1. Claim the NAME first: that needs no hardware at all.
        self._locks.append(hwlock.claim(dev_name, MODULE))
        try:
            # 2. Ask DAQmx who this is. A query, not a task: nothing on the card
            #    changes. VERIFY: attribute names in the installed nidaqmx.
            dev = nidaqmx.system.Device(dev_name)
            serial = dev.serial_num
            product = str(getattr(dev, "product_type", "") or "")
            # 3. Claim the SERIAL: the physical box, whatever its alias.
            self._locks.append(hwlock.claim(serial_address(serial), MODULE))
            self._idn = f"NI {product or 'DAQ'} SN {serial_address(serial).split(':', 1)[1]} ({dev_name})"
            if product and "6001" not in product:
                # Not fatal (a 6002/6003 is a superset), but worth a line in the log.
                self._idn += f" [expected USB-6001, found {product}]"

            # 4. Tasks. VERIFY the enum names against the installed nidaqmx
            #    (DEFAULT/RSE/NRSE/DIFF exist in nidaqmx >= 0.6).
            term = {"RSE": TerminalConfiguration.RSE,
                    "NRSE": TerminalConfiguration.NRSE,      # VERIFY: the 6001 spec lists
                    "DIFF": TerminalConfiguration.DIFF}       # RSE and DIFF only
            if layout.ai:
                self._ai = nidaqmx.Task("usb6001_ai")
                for ch, t in zip(layout.ai, layout.ai_terminal):
                    # +-10 V is the ONLY range of the 6001, so min/max are fixed.
                    self._ai.ai_channels.add_ai_voltage_chan(
                        f"{dev_name}/ai{ch}", terminal_config=term[t],
                        min_val=-10.0, max_val=10.0)

            # VERIFY: creating an AO task (and not writing it) leaves the output
            # at the level it had -- DAQmx keeps AO after a task closes or a
            # process exits. If it resets to 0 V, adopt-on-start cannot hold for
            # AO on this card; tell Lukas.
            for ch in (0, 1):
                t = nidaqmx.Task(f"usb6001_ao{ch}")
                self._ao[ch] = t
                t.ao_channels.add_ao_voltage_chan(
                    f"{dev_name}/ao{ch}", min_val=-10.0, max_val=10.0)

            if layout.di:
                self._di = nidaqmx.Task("usb6001_di")
                for line in layout.di:
                    self._di.di_channels.add_di_chan(
                        daqmx_line(dev_name, line),
                        line_grouping=LineGrouping.CHAN_PER_LINE)

            # THE ONE UNAVOIDABLE CHANGE: making a line an OUTPUT means the card
            # drives it. VERIFY what level a static DO line drives when its task
            # is created / first committed and nothing was written: the line may
            # keep the pull-up level it had as an input (USB-6001 DIO power up as
            # inputs with pull-ups -- VERIFY in the spec) or go low. A line whose
            # config says initial = low/high gets that level written by the brain
            # right after this; "leave" writes nothing.
            for line in layout.do:
                t = nidaqmx.Task(f"usb6001_do_{line}")
                self._do[line] = t
                t.do_channels.add_do_chan(daqmx_line(dev_name, line),
                                          line_grouping=LineGrouping.CHAN_PER_LINE)
        except BaseException:
            # A failed open must not leave the card claimed: the next start would
            # be refused for a card nobody is driving.
            self._close_tasks()
            self._release()
            raise

    def close(self) -> None:
        # Writes NOTHING: AO and DO stay where they are (the brain has already
        # written any safe state the config asked for). Then the tasks close,
        # and only then is the card released -- the moment it is free another
        # service may open it.
        self._close_tasks()
        self._release()

    def _close_tasks(self) -> None:
        tasks = [self._ai, self._di] + list(self._ao.values()) + list(self._do.values())
        self._ai = self._di = None
        self._ao, self._do = {}, {}
        self._timing = None
        for t in tasks:
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass

    def _release(self) -> None:
        locks, self._locks = self._locks, []
        for lock in locks:
            lock.release()

    def idn(self) -> str:
        return self._idn

    # ---- analog ---------------------------------------------------------------------

    def read_ai(self, samples: int, rate_Hz: float) -> list:
        if self._ai is None:
            return []
        from nidaqmx.constants import AcquisitionType
        n = max(2, int(samples))          # n >= 2 keeps read() two-dimensional
        if self._timing != (n, float(rate_Hz)):
            # VERIFY: re-timing a stopped finite task between reads is allowed.
            self._ai.timing.cfg_samp_clk_timing(rate=float(rate_Hz),
                                                sample_mode=AcquisitionType.FINITE,
                                                samps_per_chan=n)
            self._timing = (n, float(rate_Hz))
        # VERIFY: a finite task auto-starts on read(), returns
        # [[ch0 samples], [ch1 samples], ...] for several channels (a flat list
        # for ONE channel), and is stopped again afterwards so the next read
        # starts a new acquisition.
        data = self._ai.read(number_of_samples_per_channel=n,
                             timeout=float(self.hw.timeout_s) + n / float(rate_Hz))
        try:
            self._ai.stop()
        except Exception:
            pass
        if len(self.layout.ai) == 1 and data and not isinstance(data[0], (list, tuple)):
            data = [data]
        return [sum(ch) / len(ch) for ch in data]

    def write_ao(self, channel: int, volts: float) -> None:
        self._ao[int(channel)].write(float(volts))

    # ---- digital ---------------------------------------------------------------------

    def read_di(self) -> dict:
        if self._di is None:
            return {}
        vals = self._di.read()            # VERIFY: a list of bools, one per line
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        return {line: bool(v) for line, v in zip(self.layout.di, vals)}

    def write_do(self, line: int, level: bool) -> None:
        self._do[line].write(bool(level))

    def read_do(self) -> dict:
        # VERIFY: reading a static DO task returns the level the line is DRIVEN
        # at (DAQmx supports it on many cards). Where the card refuses: None,
        # and the brain shows the line as "unknown" until the first write.
        out = {}
        for line, task in self._do.items():
            try:
                out[line] = bool(task.read())
            except Exception:
                out[line] = None
        return out
