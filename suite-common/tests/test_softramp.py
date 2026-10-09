"""softramp.py: the shared software ramp (a setter walked at a set pace)."""

from __future__ import annotations

import threading
import time

import pytest

from suite_common.softramp import SoftRamp


class Knob:
    """A fake instrument knob: remembers every value it was sent, with time."""

    def __init__(self, value=0.0, delay_s=0.0):
        self.value = value
        self.sent = []
        self.delay_s = delay_s
        self.lock = threading.Lock()

    def set(self, v):
        with self.lock:
            if self.delay_s:
                time.sleep(self.delay_s)
            self.value = v
            self.sent.append((time.monotonic(), v))

    def get(self):
        return self.value


def test_walks_to_target_at_rate():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, dt_s=0.01)
    t0 = time.monotonic()
    rid = r.start(1.0, 4.0)            # 0 -> 1 at 4 /s = 0.25 s
    assert rid == 1 and r.running
    assert r.wait(3.0)
    dt = time.monotonic() - t0
    assert k.value == 1.0              # ends EXACTLY on the target
    assert 0.2 <= dt < 1.0
    vals = [v for _, v in k.sent]
    assert vals == sorted(vals)        # monotonic, never back
    assert len(vals) >= 10             # many small steps, not one jump
    st = r.status()
    assert st["ramping"] is False and st["ramp_end"] == "done" and st["ramp_id"] == 1


def test_pace_follows_time_not_ticks():
    """A slow setter (a busy serial line) makes fewer steps, not a slower ramp."""
    k = Knob(0.0, delay_s=0.03)
    r = SoftRamp(k.set, k.get, dt_s=0.005)
    t0 = time.monotonic()
    r.start(10.0, 20.0)                # 0.5 s whatever the step cost
    r.wait(5.0)
    assert time.monotonic() - t0 < 0.9
    # each value sent lies on the straight line through the start time
    for t, v in k.sent[:-1]:
        assert v == pytest.approx(20.0 * (t - t0), abs=1.5)


def test_clamped_to_limits():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, limits=(-1.0, 0.5), dt_s=0.005)
    r.start(5.0, 50.0)
    r.wait(3.0)
    assert max(v for _, v in k.sent) == 0.5
    assert r.status()["ramp_target"] == 0.5


def test_live_limits_function():
    k = Knob(0.0)
    lim = [(-10.0, 10.0)]
    r = SoftRamp(k.set, k.get, limits=lambda: lim[0], dt_s=0.005)
    lim[0] = (-0.2, 0.2)
    r.start(-3.0, 100.0)
    r.wait(3.0)
    assert k.value == -0.2


def test_stop_ends_where_it_is():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, dt_s=0.005)
    r.start(100.0, 10.0)
    time.sleep(0.15)
    assert r.stop() is True
    n = len(k.sent)
    v = k.value
    time.sleep(0.1)
    assert len(k.sent) == n            # nothing sent after stop() returned
    assert 0.5 < v < 5.0
    assert r.status()["ramp_end"] == "stopped" and not r.running
    assert r.stop() is False           # nothing running now


def test_new_start_takes_over_from_where_it_got():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, dt_s=0.005)
    r.start(10.0, 20.0)
    time.sleep(0.1)
    rid2 = r.start(-1.0, 50.0)
    assert rid2 == 2
    r.wait(3.0)
    assert k.value == -1.0
    # no jump back to 0 at the take-over: the second walk starts where the
    # first one was
    vals = [v for _, v in k.sent]
    peak = max(vals)
    i = vals.index(peak)
    assert all(b <= a + 1e-12 for a, b in zip(vals[i:], vals[i + 1:]))


def test_refuses_bad_numbers():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get)
    for to, rate in ((float("nan"), 1.0), (1.0, float("inf")), (1.0, 0.0)):
        with pytest.raises(ValueError):
            r.start(to, rate)
    assert not r.running
    r2 = SoftRamp(k.set)               # no getter and nothing sent yet
    with pytest.raises(ValueError):
        r2.start(1.0, 1.0)
    r2.start(1.0, 100.0, start=0.9)    # an explicit start is fine
    r2.wait(2.0)


def test_setter_error_ends_the_ramp():
    calls = []

    def bad(v):
        calls.append(v)
        if len(calls) > 3:
            raise IOError("serial timeout")

    done = []
    r = SoftRamp(bad, lambda: 0.0, dt_s=0.005,
                 on_done=lambda rid, why: done.append((rid, why)))
    r.start(10.0, 5.0)
    assert r.wait(3.0)
    st = r.status()
    assert not st["ramping"] and "serial timeout" in st["ramp_error"]
    assert done and done[0][1].startswith("error")


def test_stop_from_inside_the_setter_does_not_hang():
    r = None

    def setter(v):
        if v > 0.2:
            r.stop()

    r = SoftRamp(setter, lambda: 0.0, dt_s=0.005)
    r.start(10.0, 10.0)
    assert r.wait(3.0)
    assert r.status()["ramp_end"] == "stopped"


def test_record_is_a_stream_chunk():
    k = Knob(2.0)
    r = SoftRamp(k.set, k.get, dt_s=0.01, readback=lambda: k.value + 0.001,
                 channel="frequency")
    r.start(2.0, 1.0)                  # a zero-length walk sets the value once
    r.wait(1.0)
    sid = r.stream_start()
    assert sid == 1
    r.start(3.0, 5.0)
    r.wait(3.0)
    c = r.stream_stop()
    assert set(c) >= {"id", "t", "values", "delay_s", "overflow", "now"}
    assert set(c["values"]) == {"frequency", "frequency_readback"}
    assert len(c["t"]) == len(c["values"]["frequency"]) >= 10
    assert c["values"]["frequency"][0] == 2.0      # the value at rest, first
    assert c["values"]["frequency"][-1] == 3.0
    assert c["t"] == sorted(c["t"])
    assert c["delay_s"] == {"frequency": 0.0, "frequency_readback": 0.0}
    # not recording any more
    r.start(2.0, 100.0)
    r.wait(2.0)
    assert r.stream_read()["t"] == []


def test_record_overflow_is_reported():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, dt_s=0.001, max_samples=5)
    r.stream_start()
    r.start(1.0, 5.0)
    r.wait(2.0)
    c = r.stream_read()
    assert c["overflow"] is True and len(c["t"]) <= 6


def test_read_at_rest_keeps_covering_time():
    k = Knob(0.0)
    r = SoftRamp(k.set, k.get, dt_s=0.005)
    r.start(1.0, 100.0)
    r.wait(1.0)
    r.stream_start()
    time.sleep(0.02)
    a = r.stream_read()
    time.sleep(0.02)
    b = r.stream_read()
    assert a["t"] and b["t"] and b["t"][-1] > a["t"][-1]
    assert b["values"]["commanded"][-1] == 1.0
