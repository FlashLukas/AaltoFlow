"""The scripting API (scan_core/api.py), offline.

Most tests run against the simulated instruments (`connect(simulate=True)`);
the control tests run against the fake service with the REAL control gate in
front (conftest.ControlledFake), the same fixture the one-scan tests use.
Ports 16900-16909.
"""

from __future__ import annotations

import re
import signal
import threading
import time
from datetime import datetime

import numpy as np
import pytest
import xarray as xr

from scan_core import api
from scan_core.recipe import Recipe
from scan_core.registry import Action, Gettable


def _sim(tmp_path, **kw):
    return api.connect(simulate=True, data_dir=tmp_path, log=None, **kw)


def _small(name="small map", n_field=3, n_freq=4):
    return Recipe(name=name,
                  axes=[{"type": "linear", "param": "field", "start": 0, "stop": 40,
                         "num": n_field},
                        {"type": "linear", "param": "rf_freq", "start": 800,
                         "stop": 1200, "num": n_freq}],
                  detectors=["lockin_r"])


# ─────────────────────────────── discovery ────────────────────────────────────

def test_parameters_and_describe(tmp_path):
    with _sim(tmp_path) as lab:
        assert "field" in lab.parameters("settable")
        assert "lockin_r" in lab.parameters("detector")
        assert "vna_reference" in lab.parameters("action")
        d = lab.describe("field")
        assert d["kind"] == "settable" and d["unit"] == "mT"
        assert d["limits"] == (-200.0, 200.0)
        assert lab.describe("s21")["slow"] is True
        assert lab.describe("s21")["axes"] == ["vna_freq"]
        assert lab.describe("vna_reference")["kind"] == "action"
        assert "field" in lab.summary()


def test_an_unknown_id_names_the_close_ones(tmp_path):
    with _sim(tmp_path) as lab:
        with pytest.raises(api.ParameterNotFound, match="Did you mean: field"):
            lab.set("feild", 10)
        with pytest.raises(api.ParameterNotFound, match="no module prefix"):
            lab.get("clMag.field")
        with pytest.raises(TypeError, match="is an action"):
            lab.set("vna_reference", 1)
        with pytest.raises(TypeError, match="is a parameter"):
            lab.run("field")


# ─────────────────────────────── set / get / read ─────────────────────────────

def test_set_blocks_and_read_sees_it(tmp_path):
    with _sim(tmp_path) as lab:
        assert lab.set("field", 50) == 50.0
        assert lab.read("field") == 50.0
        assert lab.registry._state.field_mT == 50.0


def test_set_refuses_instead_of_clamping(tmp_path):
    with _sim(tmp_path) as lab:
        lab.set("field", 10)
        with pytest.raises(ValueError, match="outside its limits"):
            lab.set("field", 500)            # a typo for 50 must not drive to 200
        assert lab.read("field") == 10.0
        with pytest.raises(TypeError, match="detector"):
            lab.set("lockin_r", 1)
        with pytest.raises(ValueError):
            lab.set("field", float("nan"))


def test_get_acquires_a_slow_detector_and_read_does_not(tmp_path):
    with _sim(tmp_path) as lab:
        s = lab.registry._state
        calls = []
        real = s.trigger_vna
        s.trigger_vna = lambda: (calls.append(1), real())[1]
        lab.registry.get("s21").acquire._trigger = s.trigger_vna
        trace = lab.get("s21")
        assert isinstance(trace, np.ndarray) and trace.shape == (401,)
        assert calls == [1]
        lab.read("s21")                       # the last sweep, no new one
        assert calls == [1]
        assert isinstance(lab.get("lockin_r"), float)


# ─────────────────────────────────── actions ──────────────────────────────────

def test_run_waits_and_wraps_a_failure(tmp_path):
    with _sim(tmp_path) as lab:
        lab.run("sim_autofocus")
        assert lab.registry._state.n_autofocus == 1

        def broken():
            raise RuntimeError("no peak found")
        lab.registry.add_action(Action("bad_focus", "Bad focus", broken))
        with pytest.raises(api.ActionFailed, match="bad_focus failed: no peak found"):
            lab.run("bad_focus")
        with pytest.raises(api.ParameterNotFound, match="no action"):
            lab.run("autofocuss")


def test_run_passes_arguments_over_the_defaults(tmp_path):
    seen = {}

    def demag(args=None):
        seen.update({"amplitude_A": 1.5, **(args or {})})
    with _sim(tmp_path) as lab:
        lab.registry.add_action(Action("demag", "Demag", demag))
        lab.run("demag")
        assert seen == {"amplitude_A": 1.5}
        lab.run("demag", amplitude_A=0.5)
        assert seen == {"amplitude_A": 0.5}
        with pytest.raises(api.ActionFailed, match="takes no arguments"):
            lab.run("sim_autofocus", speed=3)


# ───────────────────────────────── wait_until ─────────────────────────────────

def _flag(lab, pid, schedule):
    """A detector whose value follows `schedule(seconds since now)`."""
    t0 = time.monotonic()
    lab.registry.add(Gettable(pid, pid, "", lambda: schedule(time.monotonic() - t0)))


def test_wait_until_a_bare_flag_and_a_comparison(tmp_path):
    with _sim(tmp_path) as lab:
        _flag(lab, "stable", lambda t: t > 0.2)
        _flag(lab, "temperature", lambda t: 10.0 - 20 * t)
        assert lab.wait_until("stable", timeout_s=5, poll_s=0.02) >= 0.2
        lab.wait_until("temperature < 5", timeout_s=5, poll_s=0.02)
        assert lab.read("temperature") < 5
        _flag(lab, "busy", lambda t: t < 0.1)
        assert lab.wait_until("not busy", timeout_s=5, poll_s=0.02) >= 0.1
        assert lab.wait_until(lambda: lab.read("temperature") < 0, timeout_s=5,
                              poll_s=0.02) > 0


def test_wait_until_needs_the_condition_to_HOLD(tmp_path):
    with _sim(tmp_path) as lab:
        # true for 0.1 s, false for 0.1 s, then true for good from 0.3 s
        _flag(lab, "stable", lambda t: t < 0.1 or t > 0.3)
        waited = lab.wait_until("stable", hold_s=0.3, timeout_s=5, poll_s=0.02)
        assert waited >= 0.6 - 0.05          # the first 0.1 s did not count


def test_wait_until_times_out_loudly(tmp_path):
    with _sim(tmp_path) as lab:
        _flag(lab, "temperature", lambda t: 7.0)
        with pytest.raises(api.WaitTimeout, match=r"temperature < 5.*last value"):
            lab.wait_until("temperature < 5", timeout_s=0.2, poll_s=0.02)
        _flag(lab, "flicker", lambda t: int(t * 20) % 2 == 0)
        with pytest.raises(api.WaitTimeout, match="stay so for 1 s"):
            lab.wait_until("flicker", hold_s=1, timeout_s=0.5, poll_s=0.01)
        with pytest.raises(api.ParameterNotFound):
            lab.wait_until("tempreature < 5", timeout_s=1)
        with pytest.raises(ValueError, match="cannot read the condition"):
            lab.wait_until("temperature <", timeout_s=1)


def test_parse_condition():
    pid, test = api.parse_condition("ppms.temperature >= -1.5e1")
    assert pid == "ppms.temperature" and test(-15) and not test(-16)
    pid, test = api.parse_condition("not camera.pattern_found")
    assert test(False) and not test(True)
    assert not api.parse_condition("x")[1](float("nan"))


# ──────────────────────────────────── scans ───────────────────────────────────

def test_a_scan_is_saved_like_the_gui_saves_it(tmp_path):
    with _sim(tmp_path) as lab:
        ds = lab.scan(_small(), name="map 5K", comment="from a test", temperature_K=5)
        path = api.path_of(ds)
        assert path == lab.last_path and path.exists()
        # <data dir>/<YYYY-MM-DD>/<HHMMSS>_<name>.nc, the suite's scheme
        assert path.parent.parent == tmp_path
        assert path.parent.name == datetime.now().strftime("%Y-%m-%d")
        assert re.fullmatch(r"\d{6}_map_5K\.nc", path.name)
        assert ds["lockin_r"].shape == (3, 4)
        with xr.open_dataset(path) as f:
            assert f.attrs["name"] == "map 5K"
            assert f.attrs["comment"] == "from a test"
            assert f.attrs["temperature_K"] == 5
            assert f.attrs["saved_by"] == "scan_core.api"
            assert np.allclose(f["lockin_r"].values, ds["lockin_r"].values)
        # a second scan in the same second gets a counter, never an overwrite
        p2 = api.path_of(lab.scan(_small(), name="map 5K"))
        assert p2 != path and p2.exists()


def test_scan_from_a_file_a_dict_and_unsaved(tmp_path):
    yml = tmp_path / "r.yaml"
    _small("from file").save(yml)
    with _sim(tmp_path / "data") as lab:
        assert api.path_of(lab.scan(str(yml))).name.endswith("_from_file.nc")
        ds = lab.scan(_small("as dict").to_dict(), save=False)
        assert api.path_of(ds) is None
        assert not list((tmp_path / "data").rglob("*as_dict*"))
        with pytest.raises(api.RecipeInvalid, match="nothing was moved"):
            lab.scan({"name": "bad", "axes": [{"type": "linear", "param": "nope",
                                               "start": 0, "stop": 1, "num": 2}],
                      "detectors": ["lockin_r"]})
        with pytest.raises(ValueError, match="written by the engine"):
            lab.scan(_small(), save=False, created="yesterday")


def test_ctrl_c_aborts_the_scan_and_keeps_the_points(tmp_path):
    """A real SIGINT during the scan: abort after the point, data saved."""
    with _sim(tmp_path) as lab:
        n = {"reads": 0}
        real = lab.registry.get("lockin_r")._get

        def reading():
            n["reads"] += 1
            if n["reads"] == 5:
                signal.raise_signal(signal.SIGINT)     # the operator's Ctrl+C
            return real()
        lab.registry.get("lockin_r")._get = reading
        with pytest.raises(KeyboardInterrupt, match="aborted by Ctrl\\+C") as info:
            lab.scan(_small("interrupted", 4, 5))
        path = info.value.path
        assert path.exists()
        with xr.open_dataset(path) as f:
            v = f["lockin_r"].values.ravel()
        assert np.isfinite(v[:5]).all() and np.isnan(v[6:]).all()
        # the handler is put back: a later Ctrl+C is an ordinary interrupt
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_hard_interrupt_still_keeps_the_points(tmp_path):
    with _sim(tmp_path) as lab:
        n = {"reads": 0}
        real = lab.registry.get("lockin_r")._get

        def reading():
            n["reads"] += 1
            if n["reads"] == 7:
                raise KeyboardInterrupt
            return real()
        lab.registry.get("lockin_r")._get = reading
        with pytest.raises(KeyboardInterrupt) as info:
            lab.scan(_small("hard stop", 4, 5))
        with xr.open_dataset(info.value.path) as f:
            assert np.isfinite(f["lockin_r"].values.ravel()[:6]).all()


def test_a_scan_queue_file_runs_every_scan(tmp_path):
    from scan_core.scan_queue import QueueEntry, save_queue_file
    q = tmp_path / "q.yaml"
    save_queue_file(q, [QueueEntry("first", _small()), QueueEntry("second", _small())])
    with _sim(tmp_path) as lab:
        out = lab.scan_queue(q)
    assert [d.attrs["name"] for d in out] == ["first", "second"]


# ──────────────────── control: the script is treated like a scan ──────────────

pytest.importorskip("zmq")
from conftest import CONTROLLED_MANIFEST, ControlledFake      # noqa: E402


def _connect(svc, tmp_path):
    return api.connect(endpoints={"fake": ("127.0.0.1", svc.cmd_port)},
                       data_dir=tmp_path, log=None, name="script test")


@pytest.fixture
def svc():
    s = ControlledFake(16900, manifest=CONTROLLED_MANIFEST).start()
    yield s
    s.stop()


def test_a_set_claims_the_instrument_until_the_script_ends(svc, tmp_path):
    with _connect(svc, tmp_path) as lab:
        assert lab.parameters("settable")[0].startswith("fake.")
        lab.set("fake.rf_power", -12)
        assert lab.read("fake.rf_power") == -12
        scan = svc.lease.status()["scan"]
        assert scan and scan["label"] == "script test"
        assert scan["kind"] == "machine" and scan["role"] == "scan"
    st = svc.lease.status()
    assert st["scan"] is None and st["holder"] is None     # all given back


def test_a_set_is_refused_while_another_pc_holds_control(svc, tmp_path):
    from suite_common.control import make_identity
    trainer = make_identity("gui", "kim GUI")
    trainer["host"] = "trainer@another-pc"
    svc.lease.handle({"cmd": "take_control", "client": trainer})
    with _connect(svc, tmp_path) as lab:
        n = len(svc.sent)
        with pytest.raises(api.ControlRefused, match="another-pc"):
            lab.set("fake.rf_power", -12)
        assert not any(m.get("cmd") == "set_power" for m in svc.sent[n:])
        lab.read("fake.rf_power")                      # reading is still fine
        with pytest.raises(api.ControlRefused):
            lab.scan({"name": "p", "axes": [{"type": "linear", "param": "fake.rf_power",
                                             "start": -20, "stop": -10, "num": 3}],
                      "detectors": ["fake.measured_field"]})
        assert not any(m.get("cmd") == "set_power" for m in svc.sent[n:])


def test_a_scan_from_a_script_keeps_the_claim_and_ctrl_c_releases_at_exit(svc, tmp_path):
    rec = {"name": "power line", "axes": [{"type": "linear", "param": "fake.rf_power",
                                           "start": -20, "stop": -10, "num": 6}],
           "detectors": ["fake.measured_field"]}
    lab = _connect(svc, tmp_path)
    try:
        ds = lab.scan(rec)
        assert ds["fake.measured_field"].shape == (6,)
        # the scan did not give the instrument back: the script still holds it
        assert svc.lease.status()["scan"]["label"].startswith("script test")
        g = lab.registry.get("fake.measured_field")
        real, n = g._get, {"reads": 0}

        def reading():
            n["reads"] += 1
            if n["reads"] == 3:
                signal.raise_signal(signal.SIGINT)
            return real()
        g._get = reading
        with pytest.raises(KeyboardInterrupt) as info:
            with lab:
                lab.scan(rec, name="interrupted")
        assert info.value.path.exists()
    finally:
        lab.close()
    assert svc.lease.status()["scan"] is None


def test_a_busy_instrument_is_refused_to_a_second_script(svc, tmp_path):
    with _connect(svc, tmp_path) as one, _connect(svc, tmp_path) as two:
        one.set("fake.rf_power", -15)
        with pytest.raises(api.ControlRefused, match="busy: scan"):
            two.set("fake.rf_power", -11)
        assert one.read("fake.rf_power") == -15


def test_connect_with_nothing_running_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_discover_endpoints", lambda *a: {})
    with pytest.raises(api.NoInstruments, match="simulate=True"):
        api.connect(log=None, data_dir=tmp_path)


# ─────────────────────────── the examples really run ──────────────────────────

@pytest.mark.parametrize("script", ["temperature_series.py", "wait_then_scan.py",
                                    "sample_positions.py"])
def test_the_examples_run_in_simulation(script, tmp_path, monkeypatch):
    """Unit tests do not cover demo scripts (deploy finding, 2026-09-13): run
    them, into a scratch data folder, quietly."""
    import runpy
    from pathlib import Path
    ex = Path(__file__).resolve().parent.parent / "examples"
    monkeypatch.syspath_prepend(str(ex))
    real = api.connect
    monkeypatch.setattr(api, "connect", lambda **kw: real(
        **{**kw, "data_dir": tmp_path, "log": None}))
    runpy.run_path(str(ex / script), run_name="__main__")
    assert list(tmp_path.rglob("*.nc"))
