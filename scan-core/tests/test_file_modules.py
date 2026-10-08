"""file_modules.py: which modules a data file used (for Mission Control's
"Start for a data file..."). Synthetic datasets, attributes only; no services."""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
import pytest

from scan_core.file_modules import (is_sim_idn, model_from_idn, modules_used,
                                    real_from_entry, recipe_slugs)
from scan_core.snapshot import snapshot_attrs

#: A made-up serial: it must never come out of the helper.
SERIAL = "987654321"


def _snap_file_attrs():
    """What engine.run files for three instruments: a simulated kim, a REAL
    hf2 on another PC (slug hf2_lab2) and a clMag that reports no idn."""
    snap = {
        "kim": {"module": "kim", "config": {"motion": {"speed": 1}},
                "info": {"idn": "SIM KIM101 3-axis piezo-inertia stage (simulator)"},
                "status": {"position_x": 1.0}},
        "hf2_lab2": {"module": "hf2", "config": {},
                     "info": {"idn": f"Zurich Instruments,HF2LI,{SERIAL},dev1234"},
                     "status": {}},
        "clMag": {"module": "clMag", "config": {}, "info": {"field_lo": -90.0},
                  "status": {"field_stable": True}},
    }
    recipe = {"name": "map", "fixed": {"clMag.field": 10.0},
              "axes": [{"type": "raster",
                        "x": {"param": "kim.position_x", "start": 0, "stop": 5, "num": 3},
                        "y": {"param": "kim.position_y", "start": 0, "stop": 5, "num": 3}}],
              "detectors": ["hf2_lab2.r1"], "hooks": []}
    attrs = snapshot_attrs(snap, "2026-10-08T10:00:00")
    attrs["recipe_json"] = json.dumps(recipe)
    return attrs


def test_snapshot_file_lists_every_instrument_with_key_and_real():
    out = modules_used(_snap_file_attrs())
    assert out["source"] == "snapshot" and "error" not in out
    rows = {r["slug"]: r for r in out["modules"]}
    assert set(rows) == {"kim", "hf2_lab2", "clMag"}
    assert rows["kim"]["key"] == "kim" and rows["kim"]["real"] is False
    assert rows["hf2_lab2"]["key"] == "hf2" and rows["hf2_lab2"]["real"] is True
    assert rows["clMag"]["real"] is None             # no idn, no flag: unknown
    assert all(r["source"] == "snapshot" for r in rows.values())
    assert all(r["in_recipe"] for r in rows.values())
    # the model, never the serial
    assert rows["hf2_lab2"]["idn_model"] == "Zurich Instruments HF2LI"
    assert SERIAL not in json.dumps(out) and "dev1234" not in json.dumps(out)


def test_an_explicit_simulated_flag_wins_over_the_idn():
    assert real_from_entry({"info": {"simulated": True, "idn": "Keysight,N5222A,1,1"}}) is False
    assert real_from_entry({"status": {"simulated": False}}) is True
    assert real_from_entry({"info": {}}) is None


@pytest.mark.parametrize("idn", [
    "Rohde&Schwarz,SMB100A,SIMULATED,0.0",
    "Thorlabs PM16-121 (simulated) S/N SIM0001 fw 1.5.0",
    "SIM KIM101 3-axis piezo-inertia stage (simulator)",
    "SimCamera (synthetic closed-loop scene)",
    "SimZ (simulated KCube piezo)",
    "ELL14 (sim)",
    "LSCI,MODEL455,SIM0001,01012026 (simulated)",
])
def test_every_simulator_idn_style_reads_as_sim(idn):
    assert is_sim_idn(idn)


@pytest.mark.parametrize("idn", [
    "Rohde&Schwarz,SMB100A,123456,4.15",
    "Thorlabs PM16-121 S/N 123456789 fw 1.5.0",
    "KEITHLEY INSTRUMENTS,MODEL 2450,04512345,1.7",
    "SIMPLE DEVICE 1",          # a word that only STARTS with SIM is not the simulator
])
def test_real_idns_do_not_read_as_sim(idn):
    assert not is_sim_idn(idn)


@pytest.mark.parametrize("idn, model", [
    ("Rohde&Schwarz,SMB100A,123456,4.15", "Rohde&Schwarz SMB100A"),
    (f"Thorlabs PM16-121 S/N {SERIAL} fw 1.5.0", "Thorlabs PM16-121"),
    (f"Thorlabs KCube piezo '{SERIAL}'", "Thorlabs KCube piezo"),
    (f"Thorlabs KIM101 {SERIAL}", "Thorlabs KIM101"),
    ("Newport AG-UC2 v2.0 on COM5", "Newport AG-UC2"),
    (f"Signal Hound at USB0::0x1234::0x5678::{SERIAL}::INSTR", "Signal Hound"),
    ("", ""),
])
def test_the_model_never_carries_a_serial(idn, model):
    assert model_from_idn(idn) == model


def test_an_old_file_with_only_the_recipe():
    """Before snapshots: the slugs come from the recipe's prefixed ids, the
    key from the slug, and real/sim is unknown."""
    recipe = {"name": "old", "fixed": {"smb.rf_power": -10},
              "axes": [{"type": "linear", "param": "clMag.field", "start": 0,
                        "stop": 1, "num": 2},
                       {"type": "zip", "name": "z",
                        "members": [{"param": "piezo_lab2.x", "values": [1, 2]}]}],
              "detectors": ["hf2.r1", "hf2.theta1"],
              "hooks": [{"when": "before_scan", "action": "call",
                         "args": {"steps": [{"set": {"mag2d.angle": 45}},
                                            {"action": "vna.take_reference"}]}}],
              "output": {"dir": "C:/data/x.y", "basename": "a.b"},
              "comment": "kim.position_x is only mentioned here"}
    out = modules_used({"recipe_json": json.dumps(recipe), "name": "old"})
    assert out["source"] == "recipe" and "error" not in out
    rows = {r["slug"]: r for r in out["modules"]}
    assert set(rows) == {"smb", "clMag", "piezo_lab2", "hf2", "mag2d", "vna"}
    assert rows["piezo_lab2"]["key"] == "piezo"
    assert all(r["real"] is None and r["source"] == "recipe" for r in rows.values())


def test_recipe_slugs_ignore_unprefixed_sim_ids():
    assert recipe_slugs({"axes": [{"type": "linear", "param": "field"}],
                         "detectors": ["lockin_r"]}) == []


def test_a_recipe_module_missing_from_the_snapshot_is_still_listed():
    attrs = _snap_file_attrs()
    rec = json.loads(attrs["recipe_json"])
    rec["detectors"].append("pm16.power")         # pm16 failed to answer at start
    attrs["recipe_json"] = json.dumps(rec)
    rows = {r["slug"]: r for r in modules_used(attrs)["modules"]}
    assert rows["pm16"]["source"] == "recipe" and rows["pm16"]["real"] is None


def test_a_file_naming_no_module_says_so():
    out = modules_used({"name": "sim"})
    assert out["modules"] == [] and "error" in out


def test_an_unreadable_file_is_an_error_not_a_traceback(tmp_path):
    bad = tmp_path / "not.nc"
    bad.write_text("not netcdf", encoding="utf-8")
    out = modules_used(bad)
    assert out["modules"] == [] and out["error"].startswith("cannot read the file")


def test_the_command_line_prints_one_ascii_json_line(tmp_path):
    xr = pytest.importorskip("xarray")
    path = tmp_path / "scan.nc"
    ds = xr.Dataset({"r": ("x", np.zeros(3))}, coords={"x": [0.0, 1.0, 2.0]})
    ds.attrs.update(_snap_file_attrs())
    ds.attrs["comment"] = "\u00b5m and \u2192"      # non-ASCII must not break the pipe
    ds.to_netcdf(path, engine="h5netcdf")
    r = subprocess.run([sys.executable, "-m", "scan_core.file_modules", str(path)],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    text = r.stdout.decode("ascii")                  # raises if not ASCII
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert {m["slug"] for m in doc["modules"]} == {"kim", "hf2_lab2", "clMag"}
    assert doc["file"] == str(path)
    # a missing file: exit code 1, still JSON
    r = subprocess.run([sys.executable, "-m", "scan_core.file_modules",
                        str(tmp_path / "gone.nc")], capture_output=True, timeout=120)
    assert r.returncode == 1 and "error" in json.loads(r.stdout)
