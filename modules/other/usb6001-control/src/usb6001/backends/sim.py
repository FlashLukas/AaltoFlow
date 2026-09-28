"""Simulated hardware: a fake NI USB-6001.

It implements DaqBackend from `base`, so the brain cannot tell it apart from
the real card. What it pretends:

  * analog inputs: a slow sine plus a little noise per channel (each channel
    its own period and offset, so the panel shows eight different traces).
    With `loopback_ai` on, ai0 and ai1 read what ao0 and ao1 output -- as if a
    wire ran from each output to an input -- so a scan AO -> AI can be tested.
  * digital inputs: a slow square wave per line; with `loopback_di` on, the
    n-th input line reads the n-th output line instead.
  * the state the box is ALREADY in when we connect (`ao_start`, `do_start`),
    exactly like a real card that kept its outputs from a previous session.
    As on the real USB-6001 the AO values cannot be read back (read_ao does
    not exist), which is what the adopt-on-start test relies on.

`writes` logs every AO and DO write in order, so a test can prove that
starting the service wrote NOTHING (Lukas's adopt-on-start rule).
"""

from __future__ import annotations

import math
import random
import time

from .base import Layout


class SimulatedUsb6001:
    def __init__(self, loopback_ai: bool = True, loopback_di: bool = False,
                 ao_start=(0.0, 0.0), do_start=None, seed: int | None = None):
        self.loopback_ai = bool(loopback_ai)
        self.loopback_di = bool(loopback_di)
        # what the fake card outputs right now (hidden: the 6001 cannot read AO back)
        self._ao = [float(v) for v in ao_start]
        self._do = list(do_start) if do_start is not None else [False] * 13
        self.layout = Layout()
        self.writes: list[tuple] = []      # ("ao", ch, V) / ("do", line, level)
        self.do_readable = True            # False: pretend the card cannot read DO back
        self.fail_reads = False            # True: every read raises (hw_error tests)
        self.read_ai_calls = 0
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self.is_open = False

    # ---- lifecycle ------------------------------------------------------------------
    def open(self, layout: Layout) -> None:
        # Creating "tasks" writes nothing: whatever the box outputs, it keeps.
        self.layout = layout
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def idn(self) -> str:
        return "NI USB-6001 SIMULATED" if self.is_open else ""

    # ---- analog ---------------------------------------------------------------------
    def read_ai(self, samples: int, rate_Hz: float) -> list:
        if self.fail_reads:
            raise OSError("simulated USB read failure")
        self.read_ai_calls += 1
        # A real read takes samples / rate seconds; pretend a little of that
        # (capped, so the tests stay fast) so timing-related code is exercised.
        time.sleep(min(samples / max(rate_Hz, 1.0), 0.02))
        t = time.monotonic() - self._t0
        noise = 0.002 / math.sqrt(max(1, samples))     # averaging shrinks the noise
        out = []
        for ch in self.layout.ai:
            if self.loopback_ai and ch in (0, 1):
                v = self._ao[ch]
            else:
                v = 0.8 * math.sin(2 * math.pi * t / (6.0 + 1.7 * ch)) + 0.25 * (ch - 3.5)
            out.append(v + self._rng.gauss(0.0, noise))
        return out

    def write_ao(self, channel: int, volts: float) -> None:
        self.writes.append(("ao", int(channel), float(volts)))
        self._ao[int(channel)] = float(volts)

    # ---- digital --------------------------------------------------------------------
    def read_di(self) -> dict:
        if self.fail_reads:
            raise OSError("simulated USB read failure")
        t = time.monotonic() - self._t0
        out = {}
        outs = list(self.layout.do)
        for k, line in enumerate(self.layout.di):
            if self.loopback_di and k < len(outs):
                out[line] = bool(self._do[outs[k]])
            else:
                out[line] = int(t / (1.0 + 0.4 * k)) % 2 == 0
        return out

    def write_do(self, line: int, level: bool) -> None:
        self.writes.append(("do", int(line), bool(level)))
        self._do[int(line)] = bool(level)

    def read_do(self) -> dict:
        return {line: (bool(self._do[line]) if self.do_readable else None)
                for line in self.layout.do}
