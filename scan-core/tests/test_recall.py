"""Instrument snapshots in the data file, the recall dialog, and the run info.

Against fake services (conftest.py) on scratch ports 16700-16760, offline.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import xarray as xr

from conftest import DEMO_MANIFEST, ControlledFake
from scan_core import Recipe, build_sim_registry, run
from scan_core.lab import build_lab_registry
from scan_core.snapshot import read_snapshot


def _lab(*svcs_and_names):
    endpoints = {name: ("127.0.0.1", svc.cmd_port, svc.pub_port)
                 for svc, name in svcs_and_names}
    return build_lab_registry(include=tuple(endpoints), endpoints=endpoints,
                              prefix=True)


def _recipe(prefix="alpha"):
    return Recipe(name="snap", comment="the comment",
                  axes=[{"type": "linear", "param": f"{prefix}.rf_power",
                         "start": -10, "stop": -8, "num": 3}],
                  detectors=[f"{prefix}.measured_field"])


# ---- the snapshot in the file ----------------------------------------------

def test_every_connected_instrument_lands_in_the_file(fake_service, tmp_path):
    a = fake_service(16700, manifest=DEMO_MANIFEST, big_status=True)
    b = fake_service(16702, manifest=DEMO_MANIFEST,
                     config={"lockin": {"tc": 0.1, "order": 4}})
    reg, lab = _lab((a, "alpha"), (b, "beta"))
    try:
        reg.settings_root = tmp_path          # snapshot_include_idn: default True
        ds = run(_recipe(), reg, attrs={"sample": "S1", "operator": "op"})
    finally:
        lab.close()
    path = tmp_path / "s.nc"
    ds.to_netcdf(path)
    with xr.open_dataset(path) as f:
        attrs = dict(f.attrs)
    assert attrs["snapshot_modules"] == "alpha,beta"
    assert attrs["snapshot_time"]
    assert attrs["sample"] == "S1" and attrs["operator"] == "op"
    assert attrs["comment"] == "the comment"
    assert attrs["software_python"]
    snap = read_snapshot(path)
    # beta plays no part in the recipe and is still recorded
    assert set(snap) == {"alpha", "beta"}
    assert snap["beta"]["config"] == {"lockin": {"tc": 0.1, "order": 4}}
    assert snap["alpha"]["module"] == "fake" and snap["alpha"]["revision"] == 12345
    assert snap["alpha"]["config"]["motion"]["steps"] == [1, 2, 3]
    assert snap["alpha"]["status"]["trace"] == "<array n=5000>"   # bounded
    assert snap["alpha"]["info"]["idn"].startswith("FAKE")
    # nothing changed during the scan
    assert json.loads(attrs["snapshot_end"]) == {}


def test_a_failing_instrument_is_an_error_entry_and_the_scan_completes(fake_service, tmp_path):
    a = fake_service(16704, manifest=DEMO_MANIFEST)
    b = fake_service(16706, manifest=DEMO_MANIFEST, config_error="config broken")
    reg, lab = _lab((a, "alpha"), (b, "beta"))
    try:
        reg.settings_root = tmp_path
        ds = run(_recipe(), reg)
    finally:
        lab.close()
    assert int(ds.sizes["alpha.rf_power"]) == 3
    snap = read_snapshot(ds)
    assert "config broken" in snap["beta"]["error"]
    assert "config" not in snap["beta"] and "status" in snap["beta"]
    assert "error" not in snap["alpha"]


def test_idn_can_be_left_out_by_the_suite_setting(fake_service, tmp_path):
    from suite_common import set_setting
    set_setting("snapshot_include_idn", False, root=tmp_path)
    a = fake_service(16708, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    try:
        reg.settings_root = tmp_path
        ds = run(_recipe(), reg)
    finally:
        lab.close()
    entry = read_snapshot(ds)["alpha"]
    assert "idn" not in entry["info"]
    assert "serial" not in entry["config"]["hardware"]


def test_a_setting_changed_during_the_scan_is_in_snapshot_end(fake_service, tmp_path):
    a = fake_service(16710, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    recipe = _recipe()

    def on_point(done, total, snap):
        a.config["motion"]["preset"] = "fast"     # someone at a GUI
    try:
        reg.settings_root = tmp_path
        ds = run(recipe, reg, on_point=on_point)
    finally:
        lab.close()
    end = json.loads(ds.attrs["snapshot_end"])
    assert end == {"alpha": [["motion.preset", "slow", "fast"]]}


def test_the_simulator_has_no_snapshot_but_provenance(tmp_path):
    reg = build_sim_registry()
    ds = run(Recipe(name="t", axes=[{"type": "linear", "param": "field",
                                     "start": 0, "stop": 1, "num": 2}],
                    detectors=["lockin_r"]), reg, attrs={"project": "P"})
    assert read_snapshot(ds) == {}
    assert ds.attrs["project"] == "P" and ds.attrs["software_python"]


# ---- the recall dialog ------------------------------------------------------

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _snapshot_of(reg, tmp_path):
    reg.settings_root = tmp_path
    return run(_recipe(), reg, attrs={"sample": "S7", "operator": "Ann"})


def test_the_dialog_shows_differences_and_sends_exactly_the_ticked_keys(
        qapp, fake_service, tmp_path):
    from apps.recall import RecallDialog
    a = fake_service(16720, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    try:
        ds = _snapshot_of(reg, tmp_path)
        a.config["motion"].update(preset="fast", speed=2.5)
        a.config["hardware"]["address"] = "COM3"
        snap = read_snapshot(ds)
        snap["gamma"] = {"module": "kim", "config": {"motion": {"x": 1}}}
        dlg = RecallDialog(snap, lab=lab, attrs=dict(ds.attrs), file_label="s.nc")
        # header: what the file was, read-only
        assert dlg.header_values["sample"] == "S7"
        assert dlg.header_values["operator"] == "Ann"

        rows = {it.text(0): it for it in dlg.setting_items("alpha")}
        assert set(rows) == {"motion.preset", "motion.speed", "hardware.address"}
        assert all(it.checkState(0) == QtCore.Qt.Unchecked for it in rows.values())
        assert rows["motion.preset"].text(1) == "fast"
        assert rows["motion.preset"].text(2) == "slow"

        # "show all" lists the equal ones too, and keeps ticks
        rows["motion.preset"].setCheckState(0, QtCore.Qt.Checked)
        dlg.show_all.setChecked(True)
        assert "motion.steps" in {it.text(0) for it in dlg.setting_items("alpha")}
        dlg.show_all.setChecked(False)
        rows = {it.text(0): it for it in dlg.setting_items("alpha")}
        assert rows["motion.preset"].checkState(0) == QtCore.Qt.Checked
        rows["motion.speed"].setCheckState(0, QtCore.Qt.Checked)

        # not connected: greyed, nothing tickable
        top = [dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())]
        gamma = next(t for t in top if t.text(0).startswith("gamma"))
        assert gamma.text(1) == "not connected"
        assert all(not (it.flags() & QtCore.Qt.ItemIsUserCheckable)
                   for it in dlg.setting_items("gamma"))

        asked = []
        dlg.confirm = lambda title, text: asked.append(text) or True
        results = dlg.apply_selected()
        assert asked and "motion.preset: fast -> slow" in asked[0]
        assert a.set_configs == [{"motion": {"preset": "slow", "speed": 1.5}}]
        assert results["alpha"][0] is True
        # applied -> no longer a difference; the address is still one
        assert {it.text(0) for it in dlg.setting_items("alpha")} == {"hardware.address"}
        dlg.close()
    finally:
        lab.close()


def test_cancelled_confirmation_sends_nothing(qapp, fake_service, tmp_path):
    from apps.recall import RecallDialog
    a = fake_service(16722, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    try:
        ds = _snapshot_of(reg, tmp_path)
        a.config["motion"]["preset"] = "fast"
        dlg = RecallDialog(read_snapshot(ds), lab=lab, attrs=dict(ds.attrs))
        dlg.select_all("alpha")
        dlg.confirm = lambda *a_: False
        dlg.apply_selected()
        assert a.set_configs == []
        dlg.close()
    finally:
        lab.close()


def test_a_refused_set_config_is_reported(qapp, fake_service, tmp_path):
    from apps.recall import RecallDialog
    a = fake_service(16724, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    try:
        ds = _snapshot_of(reg, tmp_path)
        a.config["motion"]["preset"] = "fast"
        a.set_config_error = "speed out of range"
        dlg = RecallDialog(read_snapshot(ds), lab=lab, attrs=dict(ds.attrs))
        dlg.select_all("alpha")
        dlg.confirm = lambda *a_: True
        res = dlg.apply_selected()
        assert res["alpha"][0] is False and "speed out of range" in res["alpha"][1]
        assert "REFUSED" in dlg.report.text()
        dlg.close()
    finally:
        lab.close()


def test_another_pc_holding_control_is_reported_not_forced(qapp, tmp_path):
    from apps.recall import RecallDialog
    svc = ControlledFake(16730, manifest=DEMO_MANIFEST).start()
    reg, lab = _lab((svc, "alpha"))
    try:
        ds = _snapshot_of(reg, tmp_path)
        svc.config["motion"]["preset"] = "fast"
        other = {"id": "other", "kind": "gui", "name": "kim GUI",
                 "host": "someone@OTHER-PC"}
        assert svc.lease.handle({"cmd": "take_control", "client": other})["ok"]
        dlg = RecallDialog(read_snapshot(ds), lab=lab, attrs=dict(ds.attrs))
        dlg.select_all("alpha")
        dlg.confirm = lambda *a_: True
        res = dlg.apply_selected()
        assert res["alpha"][0] is False
        assert "read-only" in res["alpha"][1] and "Control tab" in res["alpha"][1]
        assert svc.set_configs == []
        dlg.close()
    finally:
        lab.close()
        svc.stop()


def test_a_different_module_under_the_same_name_is_not_recallable(qapp, fake_service, tmp_path):
    from apps.recall import RecallDialog
    a = fake_service(16726, manifest=DEMO_MANIFEST)
    reg, lab = _lab((a, "alpha"))
    try:
        ds = _snapshot_of(reg, tmp_path)
        snap = read_snapshot(ds)
        snap["alpha"]["module"] = "kim"
        a.config["motion"]["preset"] = "fast"
        dlg = RecallDialog(snap, lab=lab, attrs=dict(ds.attrs))
        assert dlg.live["alpha"]["state"] == "other"
        assert all(not (it.flags() & QtCore.Qt.ItemIsUserCheckable)
                   for it in dlg.setting_items("alpha"))
        dlg.close()
    finally:
        lab.close()


# ---- the run info -----------------------------------------------------------

def test_run_info_persists_across_a_suite_restart_and_lands_in_the_file(
        qapp, tmp_path, monkeypatch):
    import apps.control_panel as cp
    from apps.scan_builder import ScanWorker
    from apps.suite import Suite
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")

    win = Suite(root=tmp_path)
    card = win.builder.run_info
    card.edits["sample"].setText("YIG  disc")
    card.edits["tags"].setText("fmr,yig , ")
    card.edits["comment"].setText("one comment")
    card.edits["series"].setText("S-3")
    for e in card.edits.values():
        e.editingFinished.emit()
    assert card.edits["tags"].text() == "fmr, yig"      # normalised on edit
    win.close()

    win = Suite(root=tmp_path)                           # a new launch
    try:
        vals = win.builder.run_info.values()
        assert vals["sample"] == "YIG disc" and vals["series"] == "S-3"
        assert vals["comment"] == "one comment" and vals["operator"] == ""
        b = win.builder
        b.add_axis("field")
        recipe = b.build_recipe()
        assert recipe.comment == "one comment"           # one comment field
        path = tmp_path / "run.nc"
        worker = ScanWorker(recipe, b.registry, save_path=path,
                            attrs=b.run_info.attrs())
        worker.run()                                      # in this thread
        with xr.open_dataset(path) as f:
            attrs = dict(f.attrs)
        assert attrs["sample"] == "YIG disc"
        assert attrs["tags"] == "fmr, yig"
        assert attrs["series"] == "S-3"
        assert attrs["comment"] == "one comment"
        for empty in ("operator", "project", "structure"):
            assert empty not in attrs                     # omitted when empty
    finally:
        win.close()


def test_run_info_fields_list_the_values_in_the_data_folder(tmp_path):
    """Lukas, 2026-10-05: sample / structure / operator / project / series as
    lists filled from the files in the folder; tags via a '+' menu."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apps.run_info_card import RunInfoCard
    from scan_core.autosave import write_dataset
    from scan_core.engine import run
    from scan_core.recipe import Recipe
    from scan_core.registry import build_sim_registry
    reg = build_sim_registry()
    for k, (sample, op, tags) in enumerate([("B7", "anna", "fmr, cryo"), ("Y12", "ben", "map")]):
        r = Recipe(name=f"s{k}", axes=[{"type": "linear", "param": "field",
                                        "start": 0, "stop": 1, "num": 2}],
                   detectors=["lockin_r"])
        ds = run(r, reg, attrs={"sample": sample, "operator": op, "tags": tags})
        write_dataset(ds, tmp_path / "data" / "2026-10-05" / f"12000{k}_s{k}.nc")
    card = RunInfoCard(root=tmp_path)
    card.edits["sample"].setText("typed")
    card.set_data_dir(tmp_path / "data")
    card.refresh_suggestions(wait=True)
    box = card.boxes["sample"]
    assert [box.itemText(i) for i in range(box.count())] == ["B7", "Y12"]
    assert card.edits["sample"].text() == "typed"          # typing is kept
    box.setCurrentIndex(1); box.activated.emit(1)
    assert card.values()["sample"] == "Y12"
    assert card.known_tags == ["cryo", "fmr", "map"]
    card.edits["tags"].setText("fmr")
    card.add_tag("map"); card.add_tag("FMR")               # appended once, not twice
    assert card.values()["tags"] == "fmr, map"
