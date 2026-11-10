"""The two-wire resistance measurement (config group `resistance`).

Blue Box, 2026-11-10: the antenna's DC resistance (~15 ohm) through a bias tee,
with AO0 driving a reference resistor and AI4 reading the node. These tests
pin the arithmetic (offset cancellation, open circuit), the sequence on the
card (+V, -V, back to 0 V -- also when a read fails), the lead calibration,
the config checks and the wire/describe surface. The simulator's SimLoad is
a 2200-ohm reference into 16.2 ohm with a 3 mV input offset.
"""

import math
import os
import time

import pytest

from usb6001.config import Config
from usb6001.daq import _resistance_from
from usb6001.sim_system import build_sim_system

CMD, PUB = 17758, 17759


def _cfg(**over) -> Config:
    cfg = Config()
    cfg.resistance.enabled = True
    for k, v in over.items():
        setattr(cfg.resistance, k, v)
    return cfg


def _started(cfg=None, **sim):
    cfg = cfg or _cfg()
    daq, backend = build_sim_system(cfg, seed=3, **sim)
    daq.start()
    return daq, backend


# ---- the arithmetic ---------------------------------------------------------------

def test_resistance_from_cancels_a_common_offset():
    # 9 V into 2200 + 15 ohm: node = 9 * 15 / 2215; plus 5 mV offset both ways
    node = 9.0 * 15.0 / 2215.0
    r = _resistance_from([node + 0.005] * 3, [-node + 0.005] * 3, 9.0, 2200.0, 0.0)
    assert r["total_ohm"] == pytest.approx(15.0, rel=1e-9)
    assert r["offset_V"] == pytest.approx(0.005)
    assert r["current_mA"] == pytest.approx(9.0 / 2215.0 * 1e3)
    assert r["sigma_ohm"] == 0.0                   # identical reads: no scatter
    r = _resistance_from([node], [-node], 9.0, 2200.0, 1.5)
    assert r["ohm"] == pytest.approx(13.5) and math.isnan(r["sigma_ohm"])


def test_an_open_circuit_says_so():
    r = _resistance_from([9.0, 9.0], [-9.0, -9.0], 9.0, 2200.0, 0.0)
    assert math.isinf(r["ohm"]) and "open circuit" in r["error"]


# ---- on the (simulated) card --------------------------------------------------------

def test_a_measurement_drives_plus_minus_then_zero_and_finds_the_load():
    daq, sim = _started()
    try:
        assert sim.writes == []                      # nothing at start (adopt)
        r = daq.read_resistance()
        assert r["error"] == ""
        assert r["total_ohm"] == pytest.approx(16.2, abs=0.05)
        assert r["current_mA"] == pytest.approx(9.0 / 2216.2 * 1e3, rel=1e-3)
        assert r["offset_V"] == pytest.approx(0.003, abs=5e-4)
        assert sim.writes == [("ao", 0, 9.0), ("ao", 0, -9.0), ("ao", 0, 0.0)]
        st = daq.status()
        assert st.ao_V[0] == 0.0 and st.ao_known[0]          # current off, and known
        assert st.res_id == r["res_id"] == 1 and not st.res_running
        assert st.resistance["ohm"] == r["ohm"]
    finally:
        daq.shutdown()


def test_zero_leads_is_stored_saved_and_subtracted(tmp_path):
    path = tmp_path / "usb6001.ini"
    daq, sim = _started()
    daq.config_path = str(path)
    try:
        sim.load.load_ohm = 1.2                       # antenna end shorted
        z = daq.read_resistance(zero_leads=True)
        assert z["zero_leads"] and z["total_ohm"] == pytest.approx(1.2, abs=0.05)
        assert daq.cfg.resistance.r_leads_ohm == pytest.approx(z["total_ohm"])
        assert Config.load(str(path)).resistance.r_leads_ohm == pytest.approx(z["total_ohm"])
        sim.load.load_ohm = 16.2                      # antenna back
        r = daq.read_resistance()
        assert r["ohm"] == pytest.approx(15.0, abs=0.07)
    finally:
        daq.shutdown()


def test_a_failed_read_still_returns_the_output_to_zero():
    daq, sim = _started()
    try:
        sim.fail_reads = True                         # the poll just shows hw_error
        rid = daq.measure_resistance()
        t0 = time.monotonic()
        while daq.status().res_running and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        st = daq.status()
        assert st.res_id == rid and "simulated USB read failure" in st.resistance["error"]
        assert sim.writes[-1] == ("ao", 0, 0.0) and st.ao_V[0] == 0.0
    finally:
        sim.fail_reads = False
        daq.shutdown()


def test_refusals():
    daq, _ = _started(Config())                      # resistance off
    try:
        with pytest.raises(ValueError, match="off"):
            daq.measure_resistance()
        daq.cfg.resistance.enabled = True             # on, but ai4 is not running
        daq.cfg.resistance.ai = "ai6"
        with pytest.raises(ValueError, match="restart"):
            daq.measure_resistance()
    finally:
        daq.shutdown()


def test_one_run_at_a_time():
    cfg = _cfg(settle_ms=200.0)
    daq, _ = _started(cfg)
    try:
        daq.measure_resistance()
        with pytest.raises(ValueError, match="still running"):
            daq.measure_resistance()
    finally:
        daq.shutdown()


# ---- config -------------------------------------------------------------------------

def test_sanitise_enables_the_sense_input_and_keeps_the_ao_under_5_mA():
    from usb6001.config import sanitise
    cfg = _cfg(excitation_V=10.0, r_ref_ohm=1000.0, ao="AO1", ai="AI6")
    msgs = sanitise(cfg)
    r = cfg.resistance
    assert (r.ao, r.ai) == ("ao1", "ai6")
    assert r.excitation_V == pytest.approx(5.0)       # 5 mA x 1 kohm
    assert any("5 mA" in m for m in msgs)
    assert cfg.ai.channels[6].enabled and cfg.ai.channels[6].terminal == "RSE"


def test_a_sense_input_taken_by_a_diff_pair_switches_it_off():
    from usb6001.config import sanitise
    cfg = _cfg(ai="ai4")
    cfg.ai.channels[0].terminal = "DIFF"              # ai4 = ai0's minus input
    msgs = sanitise(cfg)
    assert cfg.resistance.enabled is False and any("minus input" in m for m in msgs)


def test_resistance_settings_survive_save_and_load(tmp_path):
    cfg = _cfg(r_ref_ohm=2187.5, r_leads_ohm=1.234, reads=7, name="Antenna A")
    p = tmp_path / "x.ini"
    cfg.save(str(p))
    back = Config.load(str(p)).resistance
    assert (back.enabled, back.r_ref_ohm, back.r_leads_ohm, back.reads, back.name) == \
        (True, 2187.5, 1.234, 7, "Antenna A")


# ---- describe + wire ----------------------------------------------------------------

def test_describe_offers_the_detector_and_actions_only_when_on():
    from usb6001.net.describe import build_manifest
    daq, _ = _started()
    try:
        ids = {p["id"]: p for p in build_manifest(daq)["parameters"]}
        det = ids["resistance"]
        assert det["unit"] == "ohm" and det["acquire"]["trigger_verb"] == "measure_resistance"
        assert det["acquire"]["target_key"] == "res_id"
        assert ids["zero_leads"].get("danger") and "wait" in ids["measure_resistance"]
    finally:
        daq.shutdown()
    daq, _ = _started(Config())
    try:
        assert "resistance" not in {p["id"] for p in build_manifest(daq)["parameters"]}
    finally:
        daq.shutdown()


def test_measure_over_the_wire():
    pytest.importorskip("zmq")
    from usb6001.net.client import Usb6001Client
    from usb6001.net.service import Usb6001Service
    daq, _ = build_sim_system(_cfg(), seed=4)
    svc = Usb6001Service(daq, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    cli = Usb6001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    try:
        cli.start()
        time.sleep(0.3)
        r = cli.read_resistance(timeout_s=10)
        assert r["total_ohm"] == pytest.approx(16.2, abs=0.05) and r["error"] == ""
        rid = cli._checked({"cmd": "zero_leads"})["res_id"]
        assert rid == r["res_id"] + 1
    finally:
        cli.shutdown()
        svc.stop()
