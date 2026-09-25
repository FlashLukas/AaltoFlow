"""The module catalog, module packs, and installing a module onto a PC.

From 2026-09-24, Lukas: "how do we add a new package? ... install it from GIT
or from local folder? Like online/offline ... a wizard allowing to install the
new package based on the functionality you want". This is the part without a
screen: what a module is for, where one comes from, and what installing it does.
"""

import json
import zipfile
from pathlib import Path

import pytest

from suite_common import catalog as K
from suite_common import modules as M

from test_modules import make_module


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "suite"
    make_module(r, "kim-control", "kim", 5567, extra="")
    make_module(r, "hf2-control", "hf2", 5569)
    return r


def _cat(folder: Path, category: str, tags=()):
    """Give a made-up module a category + tags (inserted into [module])."""
    mf = folder / "module.toml"
    tag_s = ", ".join(f'"{t}"' for t in tags)
    mf.write_text(mf.read_text(encoding="utf-8").replace(
        "[module]\n", f'[module]\ncategory = "{category}"\ntags = [{tag_s}]\n', 1),
        encoding="utf-8")


# ─────────────────────────────── what a module is for ─────────────────────────

def test_category_and_tags_are_read_and_checked(tmp_path):
    d = make_module(tmp_path, "pm-control", "pm", 5601)
    assert M.parse_manifest(d / "module.toml").category == "other"   # the default
    _cat(d, "detector", ["power meter", "Thorlabs"])
    spec = M.parse_manifest(d / "module.toml")
    assert (spec.category, spec.tags) == ("detector", ["power meter", "Thorlabs"])

    bad = make_module(tmp_path, "x-control", "x", 5603)
    _cat(bad, "detectors")                    # a typo is an ERROR, not a new category
    with pytest.raises(M.ManifestError, match="not one of"):
        M.parse_manifest(bad / "module.toml")


def test_every_committed_module_says_what_it_is_for(monkeypatch):
    monkeypatch.delenv("AALTOFLOW_ROOT", raising=False)
    monkeypatch.delenv("TRMOKE_ROOT", raising=False)
    mods, problems = M.discover_local(M.default_root())
    assert problems == []
    lazy = [m.key for m in mods if m.category == "other" or not m.tags]
    assert lazy == [], "give these a category and tags in module.toml"


def test_the_committed_catalog_is_up_to_date(monkeypatch):
    """catalog.json is GENERATED (tools/make_catalog.py); a module.toml edited
    without regenerating it would advertise stale facts."""
    monkeypatch.delenv("AALTOFLOW_ROOT", raising=False)
    monkeypatch.delenv("TRMOKE_ROOT", raising=False)
    root = M.default_root()
    committed = (root / K.CATALOG_FILE).read_text(encoding="utf-8")
    assert committed == K.catalog_text(M.discover_local(root)[0]), \
        "run: python tools/make_catalog.py"


def test_search_by_function_and_words(root):
    _cat(root / "hf2-control", "detector", ["lock-in", "Zurich"])
    _cat(root / "kim-control", "motion", ["piezo"])
    mods = M.discover_local(root)[0]
    assert [m.key for m in K.search(mods, category="detector")] == ["hf2"]
    assert [m.key for m in K.search(mods, "LOCK-IN")] == ["hf2"]
    assert [m.key for m in K.search(mods, "piezo", "detector")] == []
    doc = K.build_catalog(mods)
    assert doc["categories"]["detector"]["label"] == "Detectors & analyzers"
    assert {m["key"]: m["category"] for m in doc["modules"]} == {"kim": "motion",
                                                                   "hf2": "detector"}


# ─────────────────────────────── where one comes from ─────────────────────────

def test_a_folder_of_modules_or_a_single_module_folder(tmp_path):
    shop = tmp_path / "shop"
    make_module(shop, "pm-control", "pm", 5601)
    make_module(shop, "vna-control", "vna", 5603)
    with K.ModuleSource(shop) as src:
        assert sorted(m.key for m in src.modules) == ["pm", "vna"]
        assert src.pack is None and src.wheels is None
    with K.ModuleSource(shop / "pm-control") as src:
        assert [m.key for m in src.modules] == ["pm"]


def _zip_dir(folder: Path, zpath: Path, prefix=""):
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in folder.rglob("*"):
            if p.is_file():
                zf.write(p, prefix + p.relative_to(folder).as_posix())


def test_a_module_pack_with_its_packages(tmp_path):
    stage = tmp_path / "stage"
    make_module(stage, "pm-control", "pm", 5601)
    (stage / "wheels").mkdir()
    (stage / "wheels" / "pm-requirements.txt").write_text("pyzmq==26.0\n")
    (stage / "wheels" / "pyzmq-26.0-cp314-win_amd64.whl").write_bytes(b"x")
    K.write_pack_json(stage, M.discover_local(stage)[0], "abc1234", "3.14")
    _zip_dir(stage, tmp_path / "pm.zip")

    src = K.ModuleSource(tmp_path / "pm.zip")
    try:
        assert [m.key for m in src.modules] == ["pm"]
        assert src.pack["commit"] == "abc1234" and src.pack["offline"] is True
        assert src.offline_for("pm") and not src.offline_for("vna")
        extracted = src.base
    finally:
        src.close()
    assert not extracted.exists()             # the temporary unpack is cleaned up


def test_a_zip_cannot_write_outside_its_folder(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../outside.txt", "gotcha")
    with pytest.raises(ValueError, match="unsafe path"):
        K.ModuleSource(z)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_a_broken_module_in_a_source_is_reported_not_raised(tmp_path):
    shop = tmp_path / "shop"
    make_module(shop, "pm-control", "pm", 5601)
    (shop / "pm-control" / "scripts" / "run_service.py").unlink()
    with K.ModuleSource(shop) as src:
        assert src.modules == [] and "does not exist" in src.problems[0]


# ─────────────────────────────── installing ───────────────────────────────────

def test_plan_new_update_and_conflicts(root, tmp_path):
    shop = tmp_path / "shop"
    make_module(shop, "pm-control", "pm", 5601)             # new
    make_module(shop, "kim-control", "kim", 5567)           # same folder, same key
    make_module(shop, "lockin-control", "hf2", 5569)        # hf2 lives elsewhere here
    make_module(shop, "hf2-control", "other", 5611)         # folder holds another key
    with K.ModuleSource(shop) as src:
        plans = {p.spec.dir.name: p for p in K.plan_install(src.modules, root)}
    assert plans["pm-control"].action == "new" and plans["pm-control"].ports is None
    assert plans["kim-control"].action == "update"
    assert "kept" in plans["kim-control"].reason
    assert plans["lockin-control"].action == "conflict"
    assert "hf2-control" in plans["lockin-control"].reason
    assert plans["hf2-control"].action == "conflict"
    with K.ModuleSource(root / "kim-control") as src:       # the installed copy itself
        assert K.plan_install(src.modules, root)[0].action == "same"


def test_a_new_module_whose_ports_are_taken_gets_free_ones(root, tmp_path):
    shop = tmp_path / "shop"
    make_module(shop, "a-control", "a", 5567)               # kim's ports
    make_module(shop, "b-control", "b", 5569)               # hf2's ports
    with K.ModuleSource(shop) as src:
        plans = K.plan_install(src.modules, root)
        assert [p.ports for p in plans] == [(5555, 5556), (5557, 5558)]   # and distinct
        for p in plans:
            K.install(p, root)
    found = M.discover(root)
    assert (found.by_key("a").cmd, found.by_key("b").cmd) == (5555, 5557)
    assert M.port_conflicts(found.modules) == []


def test_install_copies_code_and_keeps_the_rigs_files(root, tmp_path):
    kim = root / "kim-control"
    (kim / "kim.ini").write_text("tuned on the rig")
    (kim / "px_calibration.json").write_text("{\"x\": 21.1}")
    (kim / "Calibrations").mkdir()
    (kim / "Calibrations" / "2026-09-01.json").write_text("rig")
    (kim / ".venv").mkdir()
    (kim / ".venv" / "marker").write_text("keep me")

    shop = tmp_path / "shop"
    new = make_module(shop, "kim-control", "kim", 5567, name="Kim v2")
    (new / "kim.ini").write_text("factory default")
    (new / "px_calibration.json").write_text("{}")
    (new / "Calibrations").mkdir()
    (new / "Calibrations" / "2026-09-01.json").write_text("factory")
    (new / "Calibrations" / "README.md").write_text("new file")
    (new / "src").mkdir()
    (new / "src" / "kim.py").write_text("v2")
    (new / ".venv").mkdir()
    (new / ".venv" / "junk").write_text("never copied")
    (new / "src" / "__pycache__").mkdir()
    (new / "src" / "__pycache__" / "kim.cpython-314.pyc").write_bytes(b"x")

    with K.ModuleSource(shop) as src:
        (plan,) = K.plan_install(src.modules, root)
        log = K.install(plan, root)
    assert (kim / "kim.ini").read_text() == "tuned on the rig"
    assert (kim / "px_calibration.json").read_text() == "{\"x\": 21.1}"
    assert (kim / "Calibrations" / "2026-09-01.json").read_text() == "rig"
    assert (kim / "Calibrations" / "README.md").read_text() == "new file"   # new: added
    assert (kim / "src" / "kim.py").read_text() == "v2"
    assert M.parse_manifest(kim / "module.toml").name == "Kim v2"
    assert (kim / ".venv" / "marker").exists() and not (kim / ".venv" / "junk").exists()
    assert not (kim / "src" / "__pycache__").exists()
    assert any("kept your kim.ini" in line for line in log)


def test_a_conflict_is_never_installed(root, tmp_path):
    shop = tmp_path / "shop"
    make_module(shop, "lockin-control", "hf2", 5569)
    with K.ModuleSource(shop) as src:
        (plan,) = K.plan_install(src.modules, root)
        with pytest.raises(ValueError, match="cannot install"):
            K.install(plan, root)
    assert not (root / "lockin-control").exists()


def test_lab_data_rule_matches_the_installer():
    for rel in ("kim.ini", "KIM.INI", "px_calibration.json", "Calibrations/a.json",
                "Calibrations/sub/b.txt"):
        assert K.is_lab_data(rel), rel
    for rel in ("module.toml", "src/kim/config.ini", "tests/calibration_test.json",
                "pyproject.toml"):
        assert not K.is_lab_data(rel), rel


# ─────────────────────────────── building the environment ─────────────────────

def test_online_build_names_every_extra(root):
    d = root / "kim-control"
    (d / "pyproject.toml").write_text(
        '[project]\nname="kim"\nversion="1.2"\n[project.optional-dependencies]\n'
        'gui=["PySide6"]\nreal=["pylablib"]\n', encoding="utf-8")
    (step,) = K.env_steps(d, root, "uv", offline=False)
    assert step.argv == ["uv", "sync", "--extra", "gui", "--extra", "real"]
    assert step.cwd == d


def test_offline_build_never_touches_an_index(root):
    d = root / "kim-control"
    with pytest.raises(FileNotFoundError, match="--wheels"):
        K.env_steps(d, root, "uv", offline=True)
    (root / K.WHEELHOUSE).mkdir()
    (root / K.WHEELHOUSE / "kim-requirements.txt").write_text("pyzmq==26.0\n")
    steps = K.env_steps(d, root, "uv", offline=True)
    assert [s.argv[1] for s in steps] == ["venv", "pip", "pip"]
    for s in steps[1:]:
        assert "--no-index" in s.argv and "--offline" in s.argv
    assert steps[-1].argv[-2:] == ["-e", "."]


def test_pack_wheels_are_added_to_the_wheelhouse(root, tmp_path):
    stage = tmp_path / "stage"
    make_module(stage, "pm-control", "pm", 5601)
    (stage / "wheels").mkdir()
    (stage / "wheels" / "pm-requirements.txt").write_text("pyzmq==26.0\n")
    (stage / "wheels" / "pyzmq-26.0-cp314-win_amd64.whl").write_bytes(b"x")
    with K.ModuleSource(stage) as src:
        (plan,) = K.plan_install(src.modules, root)
        K.install(plan, root, wheels=src.wheels)
    house = root / K.WHEELHOUSE
    assert (house / "pm-requirements.txt").is_file()
    assert (house / "pyzmq-26.0-cp314-win_amd64.whl").is_file()
    assert json.loads(json.dumps(K.catalog_entry(plan.spec)))["key"] == "pm"
