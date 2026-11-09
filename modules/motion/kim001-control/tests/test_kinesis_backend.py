"""KinesisKim threading, caching and the single channel, against a fake KIM001
(no hardware, no pylablib).

The real controller is one request/reply byte stream. Two threads interleaving
on it desynchronised the link on the lab KIM101 (2026-09-13): the service's
status publisher and its command thread both talk to the backend. The fake
below FAILS if two calls ever overlap, which is what the real link does too --
just less politely.
"""

from __future__ import annotations

import threading
import time
from collections import namedtuple

import pytest

from kim001.backends.kinesis_kim001 import KinesisKim
from kim001.config import Config, NotFitted

DriveParams = namedtuple("DriveParams", "max_voltage velocity acceleration")
DeviceInfo = namedtuple("DeviceInfo", "serial_no model_no fw_ver hw_type hw_ver mod_state "
                                      "nchannels notes")

Z = 2


class FakeKim001:
    """One channel (1). Records calls; raises if a second call starts while one
    is in flight, or if any channel other than 1 is addressed."""

    def __init__(self, model="KIM001"):
        self._busy = threading.Lock()
        self.overlaps = 0
        self.calls: list[str] = []
        self.flushes = 0
        self.pos = {1: 183}
        self.drive = {1: DriveParams(112, 500, 1000)}
        self.fail_next = False
        self.model = model
        self.auto_enable: list = []

    def _enter(self, name):
        if not self._busy.acquire(blocking=False):
            self.overlaps += 1
            raise RuntimeError("unexpected channel in the reply")
        self.calls.append(name)
        time.sleep(0.0005)  # a real exchange takes time; widen the race window

    def _exit(self):
        self._busy.release()

    def _op(self, name, result=None, channel=1):
        if channel != 1:
            raise RuntimeError(f"a KIM001 has no channel {channel}")
        self._enter(name)
        try:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("unexpected channel in the reply")
            return result
        finally:
            self._exit()

    def get_device_info(self):
        return self._op("get_device_info",
                        DeviceInfo("00000001", self.model, "1.0", 0, 0, 0, 1, ""))

    def get_position(self, channel):
        return self._op("get_position", self.pos.get(channel), channel)

    def is_moving(self, channel):
        return self._op("is_moving", False, channel)

    def move_to(self, position, auto_enable=True, channel=None):
        self._op("move_to", channel=channel)
        self.auto_enable.append(auto_enable)
        self.pos[channel] = position

    def move_by(self, distance, auto_enable=True, channel=None):
        self._op("move_by", channel=channel)
        self.auto_enable.append(auto_enable)
        self.pos[channel] += distance

    def stop(self, channel, sync=True):
        return self._op("stop", channel=channel)

    def set_position_reference(self, position, channel):
        self._op("set_position_reference", channel=channel)
        self.pos[channel] = position

    def setup_drive(self, max_voltage=None, velocity=None, acceleration=None, channel=None):
        self._op("setup_drive", channel=channel)
        d = self.drive[channel]
        self.drive[channel] = DriveParams(
            d.max_voltage if max_voltage is None else max_voltage,
            d.velocity if velocity is None else velocity,
            d.acceleration if acceleration is None else acceleration,
        )

    def get_drive_parameters(self, channel):
        return self._op("get_drive_parameters", self.drive.get(channel), channel)

    def flush_comm(self):
        self.flushes += 1

    def close(self):
        pass


#: kim-control's tests import FakeKim101; same fake under the old name
FakeKim101 = FakeKim001


@pytest.fixture
def backend():
    be = KinesisKim(Config())
    be._dev = FakeKim001()  # skip open(): no pylablib needed
    return be


def test_concurrent_status_and_moves_never_overlap(backend):
    """The service's two threads, compressed: status polling vs. commands."""
    errors: list[Exception] = []
    stop = threading.Event()

    def poll_status():
        while not stop.is_set():
            try:
                backend.read_position(Z)
                backend.is_moving(Z)
                backend.read_step_rate(Z)
                backend.read_acceleration(Z)
                backend.read_voltage(Z)
            except Exception as exc:
                errors.append(exc)

    def send_moves():
        try:
            for i in range(40):
                backend.move_by(Z, 1)
                backend.move_to(Z, 100 + i)
                backend.stop(Z)
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


def test_the_focus_axis_is_channel_1_and_x_y_have_no_channel(backend):
    """Z (axis 2) is the KIM001's one channel; X / Y raise NotFitted BEFORE any
    byte goes to the controller."""
    dev = backend._dev
    backend.move_to(Z, 500)
    assert dev.pos[1] == 500
    n = len(dev.calls)
    for axis in (0, 1):
        with pytest.raises(NotFitted, match="not fitted on a KIM001"):
            backend.move_to(axis, 10)
        with pytest.raises(NotFitted):
            backend.read_position(axis)
    assert len(dev.calls) == n            # nothing was sent for X / Y


def test_moves_let_pylablib_enable_the_single_channel(backend):
    """On the KIM101 auto_enable had to be OFF (it disabled the other channel of
    the X/Y pair). A KIM001 has one channel: enabling it on demand is right."""
    backend.move_to(Z, 10)
    backend.move_by(Z, 5)
    assert backend._dev.auto_enable == [True, True]


def test_drive_parameters_are_cached_after_first_read(backend):
    dev = backend._dev
    assert backend.read_voltage(Z) == 112
    n = len(dev.calls)
    for _ in range(10):
        backend.read_step_rate(Z)
        backend.read_acceleration(Z)
        backend.read_voltage(Z)
    assert len(dev.calls) == n  # no further device traffic


def test_set_refreshes_cache_from_controller(backend):
    backend.set_voltage(Z, 85)
    backend.set_step_rate(Z, 300)
    assert backend.read_voltage(Z) == 85
    assert backend.read_step_rate(Z) == 300
    assert backend.read_acceleration(Z) == 1000  # untouched field kept


def test_failed_call_flushes_link_and_recovers(backend):
    dev = backend._dev
    dev.fail_next = True
    with pytest.raises(RuntimeError):
        backend.read_position(Z)
    assert dev.flushes == 1
    assert backend.read_position(Z) == 183  # the next call works again


def test_calls_before_open_raise_clearly():
    be = KinesisKim(Config())
    with pytest.raises(RuntimeError, match="not open"):
        be.read_position(Z)
    assert be.idn() == "Thorlabs KIM001 [closed]"


# --- no serial configured: find the one KIM001 by its (guessed) prefix ------

class _FakeThorlabs:
    def __init__(self, devices):
        self._devices = devices

    def list_kinesis_devices(self):
        return self._devices


def test_no_serial_picks_the_one_device_with_the_prefix():
    th = _FakeThorlabs([("70123456", "BSC203"), ("97654321", "Piezo Motor Controller")])
    assert KinesisKim._find_kim001(th, "97") == "97654321"
    # the prefix is a config value (hardware.serial_prefix), not a constant
    th = _FakeThorlabs([("74000001", ""), ("97654321", "")])
    assert KinesisKim._find_kim001(th, "74") == "74000001"


def test_none_or_several_matching_is_refused_by_name():
    with pytest.raises(RuntimeError, match="no KIM001 found"):
        KinesisKim._find_kim001(_FakeThorlabs([("70123456", "BSC203")]), "97")
    with pytest.raises(RuntimeError, match="97111111, 97222222"):
        KinesisKim._find_kim001(_FakeThorlabs([("97111111", ""), ("97222222", "")]), "97")
    with pytest.raises(RuntimeError, match="no KIM001 found"):
        KinesisKim._find_kim001(_FakeThorlabs([("97111111", "")]), "")   # no prefix: no guess


def test_a_kim101_is_refused_after_opening(backend):
    """If the KIM001 shares the KIM101's serial prefix, the auto-pick could open
    the SAMPLE stage's controller. The model check (a query) refuses it."""
    backend._dev = FakeKim001(model="KIM101")
    with pytest.raises(RuntimeError, match="KIM101"):
        backend._check_model()
    backend._dev = FakeKim001(model="KIM001")
    backend._check_model()                     # a KIM001 passes
