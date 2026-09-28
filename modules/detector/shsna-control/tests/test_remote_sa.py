"""The real backend against a FAKE signalhound service (tests/fake_owner.py):
the owner's TG contract over real ZeroMQ sockets on scratch ports 17700-17729.

What is proved here: we wait on the ID and not on `tg_acquiring` (gotcha #17);
the grid we file is the analyser's; a refusal / a failed sweep / an abort /
a vanished owner each end in a clear error; and we only ever abort our own
sweep."""

import itertools
import time

import numpy as np
import pytest

from fake_owner import FakeOwner
from shsna.analyzer import Analyzer
from shsna.backends.base import SweepFailed
from shsna.backends.remote_sa import OwnerLink, RemoteSa
from shsna.config import Config

_PORTS = itertools.count(17700, 2)


def _cfg(cmd, pub):
    cfg = Config()
    hw = cfg.hardware
    hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port = "127.0.0.1", cmd, pub
    hw.timeout_ms, hw.alive_s = 400, 0.5
    return cfg


def _wait(pred, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@pytest.fixture
def owner_and_backend():
    cmd = next(_PORTS)
    owner = FakeOwner(cmd, cmd + 1).start()
    be = RemoteSa(_cfg(cmd, cmd + 1))
    be.open()
    assert _wait(lambda: be.health() == ""), be.health()
    yield owner, be
    be.close()
    owner.stop()


def _run(be, start=100e6, stop=200e6, points=101, rbw=0.0, avg=1, timeout=3.0):
    be.start_sweep(start, stop, points, rbw, avg)
    assert _wait(be.poll, timeout)
    return be.fetch()


def test_a_sweep_is_filed_on_the_analysers_grid_not_the_request(owner_and_backend):
    owner, be = owner_and_backend
    r = _run(be, start=100.4e6, stop=200e6, points=201)
    assert r["points"] == 201 and r["db"].shape == (201,)
    assert r["db"][0] == pytest.approx(-19.4 - 0.2 * np.sqrt(0.1004), abs=1e-9)
    assert r["start_Hz"] == pytest.approx(100.4e6)
    # the fake starts on a whole kHz at or above the request: 100.4 MHz is one
    r = _run(be, start=100.4005e6, stop=200e6, points=5000)
    assert r["start_Hz"] == pytest.approx(100.401e6)          # NOT 100.4005 MHz
    assert r["points"] == 1001                                # the API's clamp, not 5000
    assert r["bin_Hz"] == pytest.approx((200e6 - 100.401e6) / 1000)
    req = [m for m in owner.requests if m["cmd"] == "tg_sweep_acquire"][-1]
    # no level_dbm (the TG44A ignores it in sweep mode) and rbw 0 = not sent
    assert req == {"cmd": "tg_sweep_acquire", "start_hz": 100.4005e6, "stop_hz": 200e6,
                   "points": 5000, "averages": 1}


def test_a_trace_in_another_unit_is_refused(owner_and_backend):
    """Our arithmetic is dB relative to the TG output; a dBm trace subtracted
    from a dB reference would be off by the TG's unknown level."""
    owner, be = owner_and_backend
    make = owner._make_trace
    owner._make_trace = lambda r: {**make(r), "unit": "dBm"}
    be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    assert _wait(be.poll)
    with pytest.raises(SweepFailed, match="expected 'dB'"):
        be.fetch()


def test_rbw_and_averages_are_passed_on(owner_and_backend):
    owner, be = owner_and_backend
    _run(be, rbw=3e3, avg=4)
    req = [m for m in owner.requests if m["cmd"] == "tg_sweep_acquire"][-1]
    assert req["rbw_hz"] == 3e3 and req["averages"] == 4


def test_a_stale_frame_is_neither_done_nor_failed(owner_and_backend):
    """The owner's status keeps showing the OLD ids for 0.3 s after accepting
    (fire-and-forget). With `tg_acquiring` False in those frames, a client
    trusting the flag would call the sweep finished -- or failed."""
    owner, be = owner_and_backend
    _run(be)                                        # sample #1 exists
    owner.adopt_delay_s, owner.sweep_s = 0.3, 0.02
    be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    t0 = time.monotonic()
    while not be.poll():                            # never raises on the stale frames
        time.sleep(0.01)
    assert time.monotonic() - t0 >= 0.25            # it waited for the adoption
    assert be.fetch()["points"] == 101


def test_a_failed_sweep_raises_with_the_owners_reason(owner_and_backend):
    owner, be = owner_and_backend
    owner.fail_next = "TG lost during the sweep"
    be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    with pytest.raises(SweepFailed, match="TG lost during the sweep"):
        end = time.monotonic() + 3
        while time.monotonic() < end and not be.poll():
            time.sleep(0.01)


def test_the_owner_refuses_while_a_tg_sweep_runs(owner_and_backend):
    owner, be = owner_and_backend
    owner.sweep_s = 5.0
    other = OwnerLink("127.0.0.1", owner.cmd_port, owner.pub_port, timeout_ms=400)
    other.open()
    try:
        other.rpc(cmd="tg_sweep_acquire", start_hz=1e6, stop_hz=2e6)
        with pytest.raises(SweepFailed, match="already running"):
            be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    finally:
        other.close()


def test_abort_stops_our_sweep_and_only_ours(owner_and_backend):
    owner, be = owner_and_backend
    owner.sweep_s = 5.0
    be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    be.abort()
    assert [m["cmd"] for m in owner.requests][-1] == "tg_abort"
    assert _wait(lambda: not owner.status()["tg_acquiring"])
    # a finished sweep is not ours to abort any more: no tg_abort goes out
    owner.sweep_s = 0.02
    _run(be)
    n = len(owner.requests)
    be.start_sweep(100e6, 200e6, 101, 0.0, 1)
    assert _wait(be.poll)
    _wait(lambda: be.link.cached()[0]["tg_sample_id"] == owner.status()["tg_sample_id"])
    be.abort()
    assert "tg_abort" not in [m["cmd"] for m in owner.requests[n:]]


def test_no_tracking_generator_is_hw_error_and_a_refusal(owner_and_backend):
    owner, be = owner_and_backend
    owner.tg_attached = False
    assert _wait(lambda: "no tracking generator" in be.health())
    assert be.owner_status()["tg_attached"] is False
    with pytest.raises(SweepFailed, match="no tracking generator"):
        be.start_sweep(100e6, 200e6, 101, 0.0, 1)


def test_the_owners_hw_error_reaches_health(owner_and_backend):
    owner, be = owner_and_backend
    owner.hw_error = "USB read failed"
    assert _wait(lambda: be.health() == "analyser: USB read failed")


def test_an_owner_that_is_not_running_fails_fast():
    cmd = next(_PORTS)
    be = RemoteSa(_cfg(cmd, cmd + 1))
    be.open()                                        # does not raise: nothing is sent
    try:
        assert "has not been heard" in be.health()
        assert be.owner_status()["reachable"] is False
        t0 = time.monotonic()
        with pytest.raises(ConnectionError, match="did not answer"):
            be.start_sweep(100e6, 200e6, 101, 0.0, 1)
        assert time.monotonic() - t0 < 1.5           # one timeout (0.4 s), not forever
        t0 = time.monotonic()
        with pytest.raises(ConnectionError, match="not answering"):
            be.start_sweep(100e6, 200e6, 101, 0.0, 1)
        assert time.monotonic() - t0 < 0.1           # known down: at once
    finally:
        be.close()


def test_an_owner_that_dies_mid_sweep_is_an_error_not_a_hang():
    cmd = next(_PORTS)
    owner = FakeOwner(cmd, cmd + 1, sweep_s=10.0).start()
    be = RemoteSa(_cfg(cmd, cmd + 1))
    be.open()
    try:
        assert _wait(lambda: be.health() == "")
        be.start_sweep(100e6, 200e6, 101, 0.0, 1)
        owner.stop()
        with pytest.raises(ConnectionError):
            end = time.monotonic() + 5
            while time.monotonic() < end:
                be.poll()
                time.sleep(0.02)
        assert "silent" in be.health()
    finally:
        be.close()


def test_the_grid_is_asked_from_the_owner_before_any_sweep(owner_and_backend):
    # tg_grid (2026-09-28): a scan can build its frequency axis before the
    # first sweep, and it is the grid the sweep then really uses
    owner, be = owner_and_backend
    g = be.predicted_grid(100e6, 200e6, 101)
    assert g is not None
    r = _run(be)
    assert g == pytest.approx((r["start_Hz"], r["bin_Hz"], r["points"]))


def test_the_grid_is_predicted_only_when_it_is_known(owner_and_backend):
    owner, be = owner_and_backend
    owner.supports_grid = False                            # an older owner
    assert be.predicted_grid(100e6, 200e6, 101) is None     # no sweep yet
    r = _run(be)
    assert be.predicted_grid(100e6, 200e6, 101) == (r["start_Hz"], r["bin_Hz"], r["points"])
    assert be.predicted_grid(100e6, 300e6, 101) is None     # another band: unknown
    assert be.predicted_grid(100e6, 200e6, 201) is None     # another point count too


def test_the_time_estimate_is_the_measured_one(owner_and_backend):
    _owner, be = owner_and_backend
    assert be.estimate_time_s(101, 1) == pytest.approx(0.3313)     # measured 0.36 s
    assert be.estimate_time_s(1001, 1) == pytest.approx(1.5013)    # measured 1.50 s
    assert be.estimate_time_s(5000, 2) == pytest.approx(2 * 1.5013)  # clamped to 1001


def test_the_remote_backend_claims_no_hardware(owner_and_backend, _private_lock_dir):
    """The owner holds the analyser's lock; a second claim here would make
    the two modules lock each other out."""
    assert not _private_lock_dir.exists() or not any(_private_lock_dir.iterdir())


# ---- the brain on the remote backend ------------------------------------------

@pytest.fixture
def remote_brain():
    cmd = next(_PORTS)
    owner = FakeOwner(cmd, cmd + 1, sweep_s=0.05).start()
    cfg = _cfg(cmd, cmd + 1)
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz = 100e6, 200e6
    cfg.acquisition.continuous = True                 # must be forced off at start
    sna = Analyzer(RemoteSa(cfg), cfg)
    sna.start()
    assert _wait(lambda: sna.status().hw_error == "", 3.0), sna.status().hw_error
    yield owner, sna
    sna.shutdown()
    owner.stop()


def _acquire(sna, reference=False, timeout=5.0):
    n = sna.take_reference() if reference else sna.acquire()
    assert _wait(lambda: sna.status().acq_id == n and not sna.status().acquiring, timeout)
    return n


def test_start_writes_nothing_and_does_not_sweep(remote_brain):
    owner, sna = remote_brain
    time.sleep(0.3)
    assert sna.status().continuous is False
    assert [m for m in owner.requests if m["cmd"] != "status"] == []


def test_reference_and_transmission_through_the_owner(remote_brain):
    owner, sna = remote_brain
    n = _acquire(sna, reference=True)
    assert sna.status().acq_error == "" and sna.status().reference["acq_id"] == n
    _acquire(sna)
    t = sna.get_trace("transmission")
    # the fake returns the same chain every time: transmission is 0 dB
    assert np.allclose(t["transmission"], 0.0)
    assert t["freqs_Hz"][0] == pytest.approx(100e6) and t["points"] == sna.cfg.sweep.points
    assert sna.get_trace("raw")["raw"][0] == pytest.approx(-19.4 - 0.2 * np.sqrt(0.1))
    np.testing.assert_allclose(sna.frequencies(), t["freqs_Hz"])
    assert sna.get_result()["peak_transmission_db"] == pytest.approx(0.0)


def test_a_failure_on_the_owner_is_latched_with_its_reason(remote_brain):
    owner, sna = remote_brain
    owner.fail_next = "TG overheated"
    n = _acquire(sna)
    st = sna.status()
    assert st.sample == {**st.sample, "acq_id": n, "failed": True}
    assert "TG overheated" in st.acq_error and "TG overheated" in st.hw_error
    with pytest.raises(ValueError, match="TG overheated"):
        sna.get_trace("raw")
    _acquire(sna)                                     # the next good one clears both
    assert sna.status().acq_error == "" and sna.status().hw_error == ""


def test_shutdown_aborts_our_running_sweep(remote_brain):
    owner, sna = remote_brain
    owner.sweep_s = 10.0
    sna.acquire()
    assert _wait(lambda: owner.status()["tg_acquiring"])
    sna.shutdown()
    assert "tg_abort" in [m["cmd"] for m in owner.requests]
    assert owner.status()["tg_acquiring"] is False


def test_the_brain_says_when_the_owner_is_gone(remote_brain):
    owner, sna = remote_brain
    owner.stop()
    assert _wait(lambda: "silent" in sna.status().hw_error, 3.0)
    assert sna.status().owner["reachable"] is False
    n = _acquire(sna, timeout=5.0)
    assert sna.status().acq_id == n and sna.status().acq_error
