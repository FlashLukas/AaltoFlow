"""FINE POWER (2026-10-07): the vernier fills the attenuator's 0.5 dB steps,
so the module delivers the level asked for (Lukas: "cant we just hide this in
the backend and just deliver power what was asked?")."""

import time

import pytest

from dssg import vernier_cal
from dssg.config import Config
from dssg.net.describe import build_manifest
from dssg.sim_system import build_sim_system


@pytest.fixture
def synth():
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    s, backend = build_sim_system(cfg)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.events, s.sim = events, backend
    s.start()
    yield s
    s.shutdown()


def wait_for(synth, pred, timeout=3.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.02)
    raise AssertionError(f"timed out; last status {synth.status()}")


def test_split_takes_the_nearest_step_and_fills_with_counts():
    att, n = vernier_cal.split(-13.73, 0.5, 2e9, -40, 5)
    assert att == -13.5
    assert n == round(-0.23 / 0.0441)                     # -5 counts
    att, n = vernier_cal.split(-13.80, 0.5, 2e9, -40, 5)
    assert att == -14.0 and n > 0                          # nearest step is below
    # never leaves the safety limits, even when the nearest step would
    att, n = vernier_cal.split(4.9, 0.5, 2e9, -40, 4.8)
    assert att <= 4.8


def test_slope_follows_the_measured_table():
    assert vernier_cal.slope_dB_per_count(2e9) == pytest.approx(0.0441)
    assert vernier_cal.slope_dB_per_count(3e9) == pytest.approx((0.0441 + 0.0450) / 2)
    assert vernier_cal.slope_dB_per_count(100e6) == pytest.approx(0.0480)   # end value
    assert vernier_cal.slope_dB_per_count(12e9) == pytest.approx(0.0600)


def test_power_is_delivered_as_asked(synth):
    synth.set_frequency(2e9)
    synth.set_power(-13.73)
    s = wait_for(synth, lambda s: s.attenuator_dBm == -13.5)
    assert s.fine_power is True
    assert s.vernier == -5
    # the published level is attenuator + vernier, within half a count
    assert abs(s.power_dBm - -13.73) <= 0.05
    # and the simulated output really is there (its curve is the measured one)
    assert synth.sim.output_dBm() == pytest.approx(-13.73, abs=0.05)
    assert any("attenuator -13.5 dBm, vernier -5" in m for _, m in synth.events)


def test_a_frequency_change_re_splits_the_same_power(synth):
    synth.set_frequency(2e9)
    synth.set_power(-13.73)
    wait_for(synth, lambda s: s.vernier == -5)
    synth.set_frequency(6e9)               # steeper slope there: fewer counts
    s = wait_for(synth, lambda s: s.frequency_Hz == 6e9 and s.vernier != -5)
    assert s.vernier == round(-0.23 / vernier_cal.slope_dB_per_count(6e9))
    assert abs(s.power_dBm - -13.73) <= 0.05


def test_the_vernier_is_not_a_control_of_its_own(synth):
    ids = {p["id"] for p in build_manifest(synth)["parameters"]}
    assert "vernier" not in ids
    with pytest.raises(ValueError, match="fine power"):
        synth.set_vernier(3)


def test_describe_lets_a_scan_ask_for_any_0p01_dB(synth):
    p = next(p for p in build_manifest(synth)["parameters"] if p["id"] == "power")
    assert p["resolution"] == 0.01
    assert p["settle"]["tol"] == 0.05


def test_fine_power_off_is_the_old_step_behaviour():
    cfg = Config()
    cfg.hardware.fine_power = False
    s, backend = build_sim_system(cfg)
    s.start()
    try:
        s.set_power(-13.73)
        st = wait_for(s, lambda st: st.power_dBm == -13.5)
        assert st.fine_power is False and st.vernier == 0
        ids = {p["id"] for p in build_manifest(s)["parameters"]}
        assert "vernier" in ids
    finally:
        s.shutdown()
