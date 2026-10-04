"""The run catalogue (scan_core/catalogue.py): an index of the data folder.

The files are written by the REAL engine against the simulator, then given the
run-info attributes the suite writes (sample, operator, tags, the instrument
snapshots ...), exactly as they would sit in a data folder. One corrupt file
and one old-format file (no run info at all) are in there too, because a real
folder has both.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run        # noqa: E402
from scan_core import catalogue as cat                        # noqa: E402


def _write(folder: Path, rel: str, *, name, fixed=None, axes=None, dets=None,
           created="2026-10-01T10:00:00", info=None, snapshots=None) -> Path:
    reg = build_sim_registry()
    rec = Recipe(name=name, comment=(info or {}).pop("comment", ""),
                 fixed=fixed or {}, detectors=dets or ["lockin_r"],
                 axes=axes or [{"type": "linear", "param": "field",
                                "start": 0, "stop": 10, "num": 3}])
    ds = run(rec, reg, created_iso=created)
    # what the snapshots branch adds to every file
    for k, v in (info or {}).items():
        ds.attrs[k] = v
    if snapshots:
        ds.attrs["snapshot_modules"] = ",".join(snapshots)
        ds.attrs["snapshot_time"] = created
        for slug, snap in snapshots.items():
            ds.attrs[f"snapshot_{slug}"] = json.dumps(snap)
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(path)
    return path


@pytest.fixture()
def folder(tmp_path):
    d = tmp_path / "data"
    _write(d, "2026-10-01/100000_fmr_map.nc", name="fmr map",
           fixed={"rf_power": 5.0}, created="2026-10-01T10:00:00",
           info={"sample": "B7", "structure": "disc 2 um", "operator": "lukas",
                 "project": "magnonics", "tags": "fmr, map", "series": "S1",
                 "comment": "first look", "setup_name": "TR-MOKE",
                 "aaltoflow_version": "0.2.0"},
           snapshots={"ppms": {"status": {"temperature": 5.02, "mode": "persistent"},
                               "settings": {"rate": 1.0}},
                      "clMag": {"status": {"field": 50.0, "output": True}}})
    _write(d, "2026-10-02/110000_cold.nc", name="cold sweep",
           fixed={"rf_power": -10.0},
           axes=[{"type": "linear", "param": "rf_freq", "start": 1000,
                  "stop": 3000, "num": 4}],
           dets=["lockin_r", "lockin_x"], created="2026-10-02T11:00:00",
           info={"sample": "B7", "operator": "anna", "project": "magnonics",
                 "tags": "fmr", "series": "S2"},
           snapshots={"ppms": {"status": {"temperature": 300.0, "mode": "driven"}}})
    _write(d, "2026-10-03/120000_other.nc", name="other sample",
           created="2026-10-03T12:00:00",
           info={"sample": "Y12", "structure": "film", "operator": "lukas",
                 "project": "yig", "tags": ["moke"], "series": "S1"})
    # an old-format file: what the engine wrote before the run info existed
    _write(d, "2026-09-01/090000_old.nc", name="old run",
           created="2026-09-01T09:00:00")
    # not a netCDF file at all
    (d / "2026-10-03" / "broken.nc").write_bytes(b"this is not a netcdf file")
    # a checkpoint being written right now: never indexed
    (d / "2026-10-03" / "130000_x.writing.nc").write_bytes(b"half")
    return d


def _names(rows):
    return sorted(r["name"] for r in rows)


# ─────────────────────────────── scanning ─────────────────────────────────────

def test_scan_indexes_every_file_and_records_the_broken_one(folder):
    c = cat.scan(folder)
    assert c["files"] == 5 and c["read"] == 5 and c["errors"] == 1
    assert (folder / "catalogue.sqlite").exists()
    rows = cat.search(folder)
    assert len(rows) == 5
    broken = next(r for r in rows if r["relpath"].endswith("broken.nc"))
    assert broken["error"], "the reason it could not be read is kept"
    good = next(r for r in rows if r["name"] == "fmr map")
    assert good["sample"] == "B7" and good["operator"] == "lukas"
    assert good["n_points"] == 3 and good["duration"] is not None
    assert good["dims"][0]["name"] == "field" and good["dims"][0]["size"] == 3
    assert good["dims"][0]["units"] == "mT"
    assert good["dims"][0]["min"] == 0 and good["dims"][0]["max"] == 10
    assert good["detectors"] == ["lockin_r"]
    assert set(good["tags"]) == {"fmr", "map"}
    assert "ppms" in good["instruments_text"] and "clMag" in good["instruments_text"]
    assert good["recipe_name"] == "fmr map"
    assert good["path"] == str(folder / "2026-10-01" / "100000_fmr_map.nc")


def test_the_old_format_file_is_indexed_without_run_info(folder):
    cat.scan(folder)
    (old,) = cat.search(folder, text="old run")
    assert old["error"] is None
    assert old["sample"] is None and old["tags"] == []
    assert old["created"] == "2026-09-01T09:00:00"


def test_detector_types_and_units_are_recorded(folder):
    cat.scan(folder)
    with sqlite3.connect(folder / "catalogue.sqlite") as con:
        units = dict(con.execute("SELECT name, units FROM detectors"))
        types = {t for (t,) in con.execute("SELECT type FROM detectors")}
    assert units["lockin_r"] == "V"
    assert "float" in types


def test_a_rescan_only_rereads_what_changed(folder):
    cat.scan(folder)
    again = cat.scan(folder)
    assert again["read"] == 0 and again["unchanged"] == 5
    # change one file, delete another
    target = folder / "2026-10-03" / "120000_other.nc"
    target.unlink()
    _write(folder, "2026-10-03/120000_other.nc", name="renamed run",
           created="2026-10-03T12:00:00", info={"sample": "Y13"})
    (folder / "2026-09-01" / "090000_old.nc").unlink()
    seen = []
    c = cat.scan(folder, progress=lambda d, n, rp: seen.append(rp))
    assert c["removed"] == 1 and c["read"] == 1
    assert seen == ["2026-10-03/120000_other.nc"]
    names = _names(cat.search(folder, include_errors=False))
    assert "renamed run" in names and "other sample" not in names
    assert "old run" not in names


def test_the_index_is_disposable(folder):
    cat.scan(folder)
    (folder / "catalogue.sqlite").unlink()
    assert cat.search(folder) == []
    cat.scan(folder)
    assert len(cat.search(folder)) == 5
    # a garbage file in its place is replaced, not trusted
    (folder / "catalogue.sqlite").write_bytes(b"garbage" * 100)
    cat.scan(folder)
    assert len(cat.search(folder)) == 5


def test_a_scan_can_be_cancelled(folder):
    c = cat.scan(folder, cancel=lambda: True)
    assert c["cancelled"] and c["read"] == 0


# ─────────────────────────────── searching ────────────────────────────────────

def test_free_text_searches_name_sample_structure_comment_tags(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, text="disc")) == ["fmr map"]       # structure
    assert _names(cat.search(folder, text="first look")) == ["fmr map"]  # comment, two words
    assert _names(cat.search(folder, text="moke")) == ["other sample"]  # tag
    assert _names(cat.search(folder, text="b7")) == ["cold sweep", "fmr map"]
    assert cat.search(folder, text="nothing-like-this") == []


def test_field_filters(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, sample="B7")) == ["cold sweep", "fmr map"]
    assert _names(cat.search(folder, operator="anna")) == ["cold sweep"]
    assert _names(cat.search(folder, project="yig")) == ["other sample"]
    assert _names(cat.search(folder, series="S1")) == ["fmr map", "other sample"]
    assert _names(cat.search(folder, sample="B7", operator="lukas")) == ["fmr map"]


def test_tags_must_all_be_present(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, tags="fmr")) == ["cold sweep", "fmr map"]
    assert _names(cat.search(folder, tags="FMR, map")) == ["fmr map"]
    assert _names(cat.search(folder, tags=["moke"])) == ["other sample"]


def test_instrument_and_detector(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, instrument="clmag")) == ["fmr map"]
    assert _names(cat.search(folder, instrument="ppms")) == ["cold sweep", "fmr map"]
    assert _names(cat.search(folder, detector="lockin_x")) == ["cold sweep"]


def test_date_range_is_inclusive_of_the_last_day(folder):
    cat.scan(folder)
    rows = cat.search(folder, date_from="2026-10-01", date_to="2026-10-02",
                      include_errors=False)
    assert _names(rows) == ["cold sweep", "fmr map"]
    assert _names(cat.search(folder, date_to="2026-09-30")) == ["old run"]


def test_newest_first(folder):
    cat.scan(folder)
    rows = cat.search(folder, include_errors=False)
    created = [r["created"] for r in rows]
    assert created == sorted(created, reverse=True)


# ─────────────────────────── where: conditions + snapshots ────────────────────

def test_where_on_snapshot_values(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, where="ppms.temperature between 4 and 6")) \
        == ["fmr map"]
    assert _names(cat.search(folder, where="ppms.temperature > 100")) == ["cold sweep"]
    assert _names(cat.search(folder, where="clMag.field == 50")) == ["fmr map"]
    assert _names(cat.search(folder, where="ppms.status.temperature < 10")) == ["fmr map"]
    assert _names(cat.search(folder, where='ppms.mode == "persistent"')) == ["fmr map"]
    assert _names(cat.search(folder, where="ppms.mode == driven")) == ["cold sweep"]
    assert _names(cat.search(folder, where="ppms.mode contains PERS")) == ["fmr map"]
    assert _names(cat.search(folder, where="clMag.output == true")) == ["fmr map"]
    assert _names(cat.search(folder, where="clMag.output == 1")) == ["fmr map"]


def test_where_on_fixed_conditions_and_run_columns(folder):
    cat.scan(folder)
    assert _names(cat.search(folder, where="rf_power == 5")) == ["fmr map"]
    assert _names(cat.search(folder, where="rf_power < 0")) == ["cold sweep"]
    assert _names(cat.search(folder, where="n_points >= 4")) == ["cold sweep"]
    both = cat.search(folder, where="rf_power > 0 and ppms.temperature between 4 and 6, "
                                    "n_points == 3")
    assert _names(both) == ["fmr map"]


def test_where_combines_with_the_other_filters(folder):
    cat.scan(folder)
    assert cat.search(folder, sample="Y12", where="ppms.temperature < 10") == []


@pytest.mark.parametrize("bad", [
    "x == 1; DROP TABLE files",
    "x == 1 OR 1=1",
    "1 == 1",
    "x ==",
    "x between 1",
    "x between a and b",
    "x === 1",
    "') OR 1=1 --",
    "x == 1 and",
])
def test_where_rejects_what_it_cannot_read(folder, bad):
    cat.scan(folder)
    with pytest.raises(cat.WhereError):
        cat.search(folder, where=bad)
    assert len(cat.search(folder)) == 5, "and the index is untouched"


@pytest.mark.parametrize("evil", [
    "'; DROP TABLE files; --",
    "%",
    "x_y_zz",
    "\" OR \"1\"=\"1",
    "B7' OR '1'='1",
])
def test_injection_in_values_is_just_text(folder, evil):
    cat.scan(folder)
    assert cat.search(folder, text=evil) == []
    assert cat.search(folder, sample=evil) == []
    assert cat.search(folder, tags=evil) == []
    assert cat.search(folder, instrument=evil) == []
    assert cat.search(folder, detector=evil) == []
    assert cat.search(folder, where=f"ppms.mode == {json.dumps(evil)}") == []
    assert len(cat.search(folder)) == 5, "every table still there"


def test_parse_where_shapes():
    assert cat.parse_where("a.b between 6 and 4") == [("a.b", "between", 4.0, 6.0)]
    assert cat.parse_where("x = 1, y contains 'a b'") == [("x", "==", 1.0),
                                                           ("y", "~", "a b")]
    assert cat.parse_where("hf2_lab2.r1 >= -1e-3") == [("hf2_lab2.r1", ">=", -1e-3)]


def test_snapshot_of_returns_the_flattened_values(folder):
    cat.scan(folder)
    snap = cat.snapshot_of(folder, folder / "2026-10-01" / "100000_fmr_map.nc")
    assert snap["ppms.status.temperature"] == pytest.approx(5.02)
    assert snap["ppms.status.mode"] == "persistent"
    assert snap["clMag.status.output"] == "true"


def test_distinct_values_for_completers(folder):
    cat.scan(folder)
    assert cat.distinct(folder, "sample") == ["B7", "Y12"]
    assert "ppms" in cat.distinct(folder, "instrument")
    with pytest.raises(ValueError):
        cat.distinct(folder, "name; DROP TABLE files")


# ─────────────────────────────────── CLI ──────────────────────────────────────

def test_cli_scan_and_search(folder, capsys):
    assert cat.main(["scan", str(folder)]) == 0
    assert "5 files" in capsys.readouterr().out
    assert cat.main(["search", str(folder), "--sample", "B7",
                     "--where", "ppms.temperature between 4 and 6", "--paths"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [str(folder / "2026-10-01" / "100000_fmr_map.nc")]
    assert cat.main(["search", str(folder), "--json", "--tags", "fmr"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert cat.main(["search", str(folder), "--where", "x =="]) == 2


def test_cli_runs_as_a_module(folder):
    import subprocess
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    out = subprocess.run([sys.executable, "-m", "scan_core.catalogue", "search",
                          str(folder), "--scan", "--text", "disc"],
                         cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "fmr map" in out.stdout and "1 run(s)" in out.stdout
