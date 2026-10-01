"""A dead serial link heals itself once the unit is back (2026-10-01).

On the office PC Windows Update replaced the FTDI driver of the generator's
USB adapter while the service ran: the COM port vanished and returned, and
every write then failed ("WriteFile failed ... Access is denied") until the
service was restarted. The brain now re-opens the link while reads fail.
"""

import time

from dssg.config import Config
from dssg.sim_system import build_sim_system
from dssg.synthesizer import Synthesizer


class FlakyLink:
    """The simulated unit behind a link that can die and come back."""

    def __init__(self, inner):
        self._inner = inner
        self.dead = False              # the port handle is gone
        self.unit_back = False         # the device has re-appeared
        self.reopens = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name.startswith("read_") or name in ("usb_volts", "external_ref_detected",
                                                "errors"):
            def guarded(*a, **k):
                if self.dead:
                    raise OSError("WriteFile failed (Access is denied)")
                return attr(*a, **k)
            return guarded
        return attr

    def reopen(self):
        self.reopens += 1
        if not self.unit_back:
            raise OSError("could not open port 'COM10'")
        self.dead = False


def _wait(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_the_link_is_reopened_when_the_unit_comes_back(monkeypatch):
    monkeypatch.setattr(Synthesizer, "REOPEN_S", 0.2)
    synth, sim = build_sim_system(Config())
    link = FlakyLink(sim)
    synth.backend = link
    events = []
    synth._on_event = lambda level, msg: events.append((level, msg))
    synth.start()
    try:
        assert _wait(lambda: synth.status().polls > 2 and not synth.status().hw_error)
        link.dead = True                               # the driver swap
        assert _wait(lambda: "Access is denied" in synth.status().hw_error)
        assert _wait(lambda: link.reopens >= 2)        # keeps trying, quietly
        assert synth.status().hw_error                 # still stale meanwhile
        link.unit_back = True                          # plugged back in
        assert _wait(lambda: not synth.status().hw_error)
        assert any("re-opened" in m for _, m in events)
        assert any("recovered" in m for _, m in events)
    finally:
        synth.shutdown()
