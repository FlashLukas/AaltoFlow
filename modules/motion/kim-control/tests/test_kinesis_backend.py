"""KinesisKim threading and caching, against a fake KIM101 (no hardware, no pylablib).

The real controller is one request/reply byte stream. Two threads interleaving
on it desynchronised the link on the lab unit (2026-09-13): the service's
status publisher and its command thread both talk to the backend. The fake
below FAILS if two calls ever overlap, which is what the real link does too --
just less politely.
"""

from __future__ import annotations

import threading
import time
from collections import namedtuple

import pytest

from kim.backends.kinesis_kim import KinesisKim
from kim.config import Config

DriveParams = namedtuple("DriveParams", "max_voltage velocity acceleration")


class FakeKim101:
    """Records calls; raises if a second call starts while one is in flight."""

    def __init__(self):
        self._busy = threading.Lock()
        self.overlaps = 0
        self.calls: list[str] = []
        self.flushes = 0
        self.pos = {1: 31, 2: 667, 3: 183}
        self.drive = {ch: DriveParams(112, 500, 1000) for ch in (1, 2, 3)}
        self.fail_next = False

    def _enter(self, name):
        if not self._busy.acquire(blocking=False):
            self.overlaps += 1
            raise RuntimeError("unexpected channel in the reply")
        self.calls.append(name)
        time.sleep(0.0005)  # a real exchange takes time; widen the race window

    def _exit(self):
        self._busy.release()

    def _op(self, name, result=None):
        self._enter(name)
        try:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("unexpected channel in the reply")
            return result
        finally:
            self._exit()

    def get_position(self, channel):
        return self._op("get_position", self.pos[channel])

    def is_moving(self, channel):
        return self._op("is_moving", False)

    # The KIM101 drives only ENABLED channels: one, or the pair (1,2) / (3,4).
    # Enabling a set that leaves out a moving channel stops it (as on hardware).
    enabled: tuple = ()
    cut_off: list = []

    def enable_channels(self, channels):
        self._op("enable_channels")
        new = tuple(channels) if isinstance(channels, (tuple, list)) else (channels,)
        self.cut_off = self.cut_off + [c for c in self.enabled if c not in new]
        self.enabled = new
        return new

    def _auto(self, channel, auto_enable):
        if auto_enable and channel not in self.enabled:
            self.enable_channels(channel)            # pylablib: JUST this channel
        if channel not in self.enabled:
            raise RuntimeError(f"channel {channel} not enabled")

    def move_to(self, position, auto_enable=True, channel=None):
        self._auto(channel, auto_enable)
        self._op("move_to")
        self.pos[channel] = position

    def move_by(self, distance, auto_enable=True, channel=None):
        self._auto(channel, auto_enable)
        self._op("move_by")
        self.pos[channel] += distance

    def stop(self, channel, sync=True):
        return self._op("stop")

    def set_position_reference(self, position, channel):
        self._op("set_position_reference")
        self.pos[channel] = position

    def setup_drive(self, max_voltage=None, velocity=None, acceleration=None, channel=None):
        self._op("setup_drive")
        d = self.drive[channel]
        self.drive[channel] = DriveParams(
            d.max_voltage if max_voltage is None else max_voltage,
            d.velocity if velocity is None else velocity,
            d.acceleration if acceleration is None else acceleration,
        )

    def get_drive_parameters(self, channel):
        return self._op("get_drive_parameters", self.drive[channel])

    def flush_comm(self):
        self.flushes += 1

    def close(self):
        pass


@pytest.fixture
def backend():
    be = KinesisKim(Config())
    be._dev = FakeKim101()  # skip open(): no pylablib needed
    return be


def test_concurrent_status_and_moves_never_overlap(backend):
    """The service's two threads, compressed: status polling vs. commands."""
    errors: list[Exception] = []
    stop = threading.Event()

    def poll_status():
        while not stop.is_set():
            try:
                for a in range(3):
                    backend.read_position(a)
                    backend.is_moving(a)
                    backend.read_step_rate(a)
                    backend.read_acceleration(a)
                    backend.read_voltage(a)
            except Exception as exc:
                errors.append(exc)

    def send_moves():
        try:
            for i in range(40):
                backend.move_by(i % 3, 1)
                backend.move_to(i % 3, 100 + i)
                backend.stop(i % 3)
        except Exception as exc:
            errors.append(exc)

    pollers = [threading.Thread(target=poll_status) for _ in range(2)]
    movers = [threading.Thread(target=send_moves) for _ in range(2)]
    for t in pollers + movers:
        t.start()
    for t in movers:
        t.join()
    stop.set()
    for t in pollers:
        t.join()

    assert backend._dev.overlaps == 0
    assert errors == []


def test_drive_parameters_are_cached_after_first_read(backend):
    dev = backend._dev
    assert backend.read_voltage(0) == 112
    n = len(dev.calls)
    for _ in range(10):
        backend.read_step_rate(0)
        backend.read_acceleration(0)
        backend.read_voltage(0)
    assert len(dev.calls) == n  # no further device traffic


def test_set_refreshes_cache_from_controller(backend):
    backend.set_voltage(1, 85)
    backend.set_step_rate(1, 300)
    assert backend.read_voltage(1) == 85
    assert backend.read_step_rate(1) == 300
    assert backend.read_acceleration(1) == 1000  # untouched field kept


def test_failed_call_flushes_link_and_recovers(backend):
    dev = backend._dev
    dev.fail_next = True
    with pytest.raises(RuntimeError):
        backend.read_position(0)
    assert dev.flushes == 1
    assert backend.read_position(0) == 31  # the next call works again


def test_calls_before_open_raise_clearly():
    be = KinesisKim(Config())
    with pytest.raises(RuntimeError, match="not open"):
        be.read_position(0)
    assert be.idn() == "Thorlabs KIM101 [closed]"


def test_x_and_y_moves_do_not_cut_each_other_off(backend):
    """2026-09-14: an X move followed by a Y move stopped X after a few steps,
    because pylablib's auto_enable enables ONLY the addressed channel. X and Y
    must run as the enabled pair (1, 2)."""
    dev = backend._dev
    backend.move_to(0, 5000)       # X, ch1
    backend.move_to(1, -20)        # Y, ch2, straight after
    backend.move_by(0, 10)
    assert dev.enabled == (1, 2)
    assert dev.cut_off == []       # nothing was disabled while moving
    n = dev.calls.count("enable_channels")
    backend.move_to(1, 0)
    assert dev.calls.count("enable_channels") == n   # pair cached, no re-enable
    backend.move_to(2, 100)        # Z lives in the other pair
    assert dev.enabled == (3, 4)


# --- no serial configured: find the one KIM101 (the public default) --------

class _FakeThorlabs:
    def __init__(self, devices):
        self._devices = devices

    def list_kinesis_devices(self):
        return self._devices


def test_no_serial_picks_the_one_kim101():
    from kim.backends.kinesis_kim import KinesisKim
    th = _FakeThorlabs([("70123456", "BSC203"), ("97654321", "KIM101")])
    assert KinesisKim._find_kim101(th) == "97654321"


def test_no_kim101_or_several_is_refused_by_name():
    import pytest
    from kim.backends.kinesis_kim import KinesisKim
    with pytest.raises(RuntimeError, match="no KIM101 found"):
        KinesisKim._find_kim101(_FakeThorlabs([("70123456", "BSC203")]))
    with pytest.raises(RuntimeError, match="97111111, 97222222"):
        KinesisKim._find_kim101(_FakeThorlabs([("97111111", ""), ("97222222", "")]))
