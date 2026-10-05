"""Snapshots carry each parameter's value under its SCAN name, so the catalogue
can be searched with the names people scan with (lab PC, 2026-10-05:
`where pm16.wavelength > 0` found nothing; only `pm16.wavelength_nm` did)."""

import pathlib

from scan_core import catalogue, lab as lab_mod, snapshot
from scan_core.autosave import write_dataset
from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import Gettable, Registry, Settable, build_sim_registry


class _Inst:
    manifest = {"module": "pm16"}


def test_parameter_values_are_added_under_their_scan_name(monkeypatch):
    monkeypatch.setattr(lab_mod, "module_prefix", lambda inst, manifest=None: "pm16")
    reg = Registry()
    reg.add(Settable("pm16.wavelength", "WL", "nm", (400, 1100), lambda v: None,
                     lambda: 610.0))
    reg.add(Gettable("pm16.power", "P", "mW", lambda: float("nan")))   # NaN skipped
    reg.add(Gettable("pm16.flag", "F", "", lambda: ""))

    class _Lab:
        instruments = {"pm16": _Inst()}
    snap = {"pm16": {"module": "pm16", "config": {"sensor": {"wavelength_nm": 610.0}}}}
    lab_mod._add_parameter_values(snap, reg, {"pm16.wavelength": "pm16",
                                              "pm16.power": "pm16",
                                              "pm16.flag": "pm16"}, _Lab())
    assert snap["pm16"]["values"] == {"wavelength": 610.0, "flag": ""}


def test_the_catalogue_finds_a_snapshot_by_parameter_id(tmp_path):
    snap = {"pm16": {"module": "pm16", "slug": "pm16", "config": {"sensor": {"wavelength_nm": 610.0}},
                     "values": {"wavelength": 610.0}}}
    reg = build_sim_registry()
    r = Recipe(name="t", axes=[{"type": "linear", "param": "field", "start": 0,
                                "stop": 1, "num": 2}], detectors=["lockin_r"])
    ds = run(r, reg, attrs=snapshot.snapshot_attrs(snap))
    write_dataset(ds, pathlib.Path(tmp_path) / "2026-10-05" / "120000_t.nc")
    catalogue.scan(tmp_path)
    assert len(catalogue.search(tmp_path, where="pm16.wavelength > 600")) == 1
    assert len(catalogue.search(tmp_path, where="pm16.wavelength > 700")) == 0
