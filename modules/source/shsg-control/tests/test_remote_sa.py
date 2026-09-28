"""The REAL backend (backends/remote_sa.py) against a FAKE signalhound service.

shsg owns no hardware: the TG44A is driven through the signalhound service.
These tests check that contract from shsg's side, offline, on test ports
17660..17669 (never the real 5587/5588):

  * adopt on start = READ the owner's tg_cw state; nothing is sent at start;
  * our status shows only what the OWNER published as applied -- never our
    request, even when the owner accepts at once and applies later (gotcha #40);
  * refusals (busy sweep, no TG, out of range) come back as clear errors;
  * owner down: start still works, status says "not reachable", commands fail
    fast; the owner coming up later is picked up by itself;
  * "unknown" TG state: nothing adopted, set explicitly clears it;
  * clean stop switches the CW off (off_on_shutdown), or leaves it (False);
  * no hwlock claim (the owner holds the USB devices, not us).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fake_owner import FakeOwner
from shsg.backends.remote_sa import RemoteTG
from shsg.config import Config
from shsg.generator import Generator, Refused
from shsg.net.protocol import status_to_dict

CMD, PUB = 17660, 17661


def _gen(cmd=CMD, pub=PUB, wait_s=3.0, **hw):
    cfg = Config()
    for k, v in hw.items():
        setattr(cfg.hardware, k, v)
    backend = RemoteTG("127.0.0.1", cmd, pub, timeout_ms=1500, wait_s=wait_s)
    g = Generator(backend, cfg)
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    return g, backend


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def owner():
    o = FakeOwner(CMD, PUB, on=True, freq_hz=2.345e9, level_dbm=-15.0).start()
    yield o
    o.stop()


def test_adopts_the_owners_state_and_sends_nothing_at_start(owner):
    g, _ = _gen()
    g.start()
    try:
        s = g.status()
        assert (s.rf_on, s.frequency_Hz, s.power_dBm) == (True, 2.345e9, -15.0)
        assert s.connected and s.hw_error == "" and not s.tg_busy and s.tg_ready
        assert owner.tg_cw_requests() == [], "something was sent at start"
        assert any("adopted" in m for _, m in g.events)
    finally:
        g.cfg.hardware.off_on_shutdown = False
        g.shutdown()


def test_commands_use_the_contract_missing_means_keep(owner):
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        g.set_frequency(1.5e9)
        g.set_power(-12.0)
        g.set_rf(False)
        sent = owner.tg_cw_requests()
        assert sent == [{"cmd": "tg_cw", "freq_hz": 1.5e9},
                        {"cmd": "tg_cw", "level_dbm": -12.0},
                        {"cmd": "tg_cw", "on": False}]
        assert _wait(lambda: (g.status().frequency_Hz, g.status().power_dBm,
                              g.status().rf_on) == (1.5e9, -12.0, False))
    finally:
        g.shutdown()


def test_status_never_shows_a_value_before_the_owner_applied_it():
    """The owner ACCEPTS at once but applies 0.6 s later. Until its published
    status carries the new value, ours must keep showing the old one -- else a
    scan waiting for the echo would accept a frequency the TG is not at."""
    o = FakeOwner(CMD + 2, PUB + 2, on=True, freq_hz=1e9, apply_delay_s=0.6).start()
    g, _ = _gen(CMD + 2, PUB + 2, off_on_shutdown=False)
    try:
        g.start()
        g.set_frequency(2e9)                    # returns: accepted
        assert g.status().frequency_Hz == 1e9   # ...but NOT applied yet
        t_seen = None
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if g.status().frequency_Hz == 2e9:
                t_seen = time.monotonic()
                break
            time.sleep(0.01)
        assert t_seen is not None, "the applied value never arrived"
        t_applied = o.applied_at[-1][0]
        assert t_seen >= t_applied, "status showed the value before the owner applied it"
    finally:
        g.shutdown()
        o.stop()


def test_busy_sweep_is_shown_and_commands_are_refused(owner):
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        owner.set(tg_mode="sweep")
        assert _wait(lambda: g.status().tg_busy)
        s = g.status()
        assert not s.tg_ready
        with pytest.raises(Refused, match="sweep"):
            g.set_frequency(3e9)
        assert owner.tg_cw_requests() == []     # our own check stopped it
        assert any(lvl == "error" and "sweep" in m for lvl, m in g.events)
        owner.set(tg_mode="cw")                 # the owner restored the CW
        assert _wait(lambda: g.status().tg_ready)
        g.set_frequency(3e9)
    finally:
        g.shutdown()


def test_owner_refusal_is_passed_on_verbatim(owner):
    """The TG may become busy between our check and the owner's: the owner is
    the authority and its words must reach the user."""
    g, backend = _gen(off_on_shutdown=False)
    g.start()
    try:
        owner.set(tg_mode="sweep")              # the owner knows at once...
        with pytest.raises(RuntimeError, match="busy: TG sweep running"):
            backend.set_cw(freq_hz=3e9)         # ...even if our cache does not yet
    finally:
        g.shutdown()


def test_no_tg_attached_is_a_hw_error_and_refused():
    o = FakeOwner(CMD + 4, PUB + 4, attached=False).start()
    g, _ = _gen(CMD + 4, PUB + 4)
    try:
        g.start()
        s = g.status()
        assert "no tracking generator attached" in s.hw_error
        assert not s.tg_ready
        with pytest.raises(Refused, match="no tracking generator"):
            g.set_rf(True)
        assert o.tg_cw_requests() == []
    finally:
        g.shutdown()
        o.stop()


def test_owner_hw_error_is_passed_on(owner):
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        owner.set(hw_error="USB read failed")
        assert _wait(lambda: g.status().hw_error == "signalhound: USB read failed")
        with pytest.raises(Refused):
            g.set_power(-20.0)
    finally:
        g.shutdown()


def test_owner_down_at_start_then_comes_up():
    """Start does not fail without the owner (the launcher starts it first, but
    opening USB takes a while): hw_error says why, commands fail FAST, and the
    owner's first frame is picked up by itself -- adopting, not writing."""
    g, _ = _gen(CMD + 6, PUB + 6, wait_s=0.3)
    o = None
    try:
        g.start()
        s = g.status()
        assert "signalhound service not reachable" in s.hw_error
        assert not s.connected and not s.tg_ready
        t0 = time.monotonic()
        with pytest.raises(Refused, match="not reachable"):
            g.set_frequency(2e9)
        assert time.monotonic() - t0 < 0.5, "a command to a dead owner must fail fast"

        o = FakeOwner(CMD + 6, PUB + 6, on=True, freq_hz=3e9, level_dbm=-11.0).start()
        assert _wait(lambda: g.status().hw_error == "", timeout=5.0)
        s = g.status()
        assert (s.rf_on, s.frequency_Hz, s.power_dBm, s.connected) == (True, 3e9, -11.0, True)
        assert o.tg_cw_requests() == []
    finally:
        g.cfg.hardware.off_on_shutdown = False
        g.shutdown()
        if o is not None:
            o.stop()


def test_owner_going_away_is_noticed(owner):
    g, backend = _gen(off_on_shutdown=False)
    backend.ALIVE_S = 0.4                       # instead of 2 s, to keep the test short
    g.start()
    try:
        owner.stop()
        assert _wait(lambda: "not reachable" in g.status().hw_error, timeout=3.0)
        with pytest.raises(Refused):
            g.set_rf(False)
    finally:
        g.shutdown()


def test_unknown_state_adopts_nothing_and_a_setting_clears_it():
    o = FakeOwner(CMD + 8, PUB + 8, mode="unknown", freq_hz=4e9, level_dbm=-10.0).start()
    g, _ = _gen(CMD + 8, PUB + 8, off_on_shutdown=False)
    try:
        g.start()
        s = g.status()
        assert s.tg_unknown and not s.tg_ready
        assert (g._freq, g._power) == (Config().signal.frequency_Hz,
                                       Config().signal.power_dBm), "adopted an unknown state"
        assert any(lvl == "warn" and "unknown" in m for lvl, m in g.events)
        assert status_to_dict(s)["tg_unknown"] is True
        g.set_rf(False)                          # an explicit setting is allowed...
        assert _wait(lambda: not g.status().tg_unknown)   # ...and makes it known
        assert g.status().tg_ready
    finally:
        g.shutdown()
        o.stop()


def test_clean_stop_switches_the_cw_off(owner):
    g, _ = _gen()                                # off_on_shutdown default True
    g.start()
    g.shutdown()
    assert owner.tg_cw_requests() == [{"cmd": "tg_cw", "on": False}]
    assert owner.state["tg_cw_on"] is False


def test_clean_stop_leaves_it_when_configured(owner):
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    g.shutdown()
    assert owner.tg_cw_requests() == []
    assert owner.state["tg_cw_on"] is True


def test_clean_stop_while_busy_warns_and_leaves_it_to_the_owner(owner):
    g, _ = _gen()
    g.start()
    owner.set(tg_mode="sweep")
    time.sleep(0.2)
    g.shutdown()                                 # must not raise
    assert any(lvl == "warn" and "could not switch the CW off" in m for lvl, m in g.events)


def test_no_hwlock_claim(owner, tmp_path, monkeypatch):
    """We own no physical address: the owner claims the USB devices."""
    locks = tmp_path / "locks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(locks))
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        assert not locks.exists() or not any(Path(locks).iterdir())
    finally:
        g.shutdown()


# ---- "off" is a PARK: the TG44A cannot be silenced (lab PC, 2026-09-28) -----

def test_parked_is_reported_honestly(owner):
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        g.set_rf(False)                                  # -> the owner parks
        assert owner.tg_cw_requests()[-1] == {"cmd": "tg_cw", "on": False}
        assert _wait(lambda: g.status().parked)
        s = g.status()
        assert s.rf_on is False and s.park_Hz == 10_000.0 and s.park_dBm == -30.0
        d = status_to_dict(s)
        assert d["parked"] is True and d["park_Hz"] == 10_000.0
        assert any("cannot be silenced" in m for _, m in g.events)
    finally:
        g.shutdown()


def test_a_parked_tg_is_never_reported_on(owner):
    """Even if the owner's tg_cw_on still says True (the CW it would restore),
    tg_mode "parked" wins: the TG is not emitting that CW."""
    g, _ = _gen(off_on_shutdown=False)
    g.start()
    try:
        owner.set(tg_mode="parked", tg_cw_on=True)
        assert _wait(lambda: g.status().parked)
        assert g.status().rf_on is False
    finally:
        g.shutdown()


def test_clean_stop_does_not_park_twice():
    o = FakeOwner(CMD + 2, PUB + 2, on=False).start()   # already parked
    g, _ = _gen(CMD + 2, PUB + 2)
    try:
        g.start()
        g.shutdown()
        assert o.tg_cw_requests() == []
    finally:
        o.stop()
