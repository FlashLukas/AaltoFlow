"""Discovery against a throwaway root full of fake modules.

Each test builds its own folder tree, so nothing here depends on (or touches)
the real suite -- and a broken real module.toml cannot make these fail.
"""

import json
from pathlib import Path

import pytest

from suite_common import modules as M


def make_module(root: Path, folder: str, key: str, cmd: int, *, name=None,
                gui=True, order=100, start_after=(), icon=True, extra=""):
    d = root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    if gui:
        (d / "scripts" / "run_gui.py").write_text("")
    if icon:
        (d / "icon.svg").write_text("<svg/>")
    after = ", ".join(f'"{k}"' for k in start_after)
    (d / "module.toml").write_text(f"""
[module]
key = "{key}"
name = "{name or key.title()}"
description = "a {key}"
icon = "icon.svg"
order = {order}

[ports]
cmd = {cmd}
pub = {cmd + 1}

[run]
service = "scripts/run_service.py"
gui = "{'scripts/run_gui.py' if gui else ''}"
start_after = [{after}]
{extra}
""", encoding="utf-8")
    return d


@pytest.fixture
def root(tmp_path):
    make_module(tmp_path, "kim-control", "kim", 5567, order=60)
    make_module(tmp_path, "camera-control", "camera", 5563, order=70,
                start_after=["kim", "piezo"])
    make_module(tmp_path, "zpiezo-control", "zpiezo", 5565, gui=False, order=50)
    (tmp_path / "scan-core").mkdir()          # not a module: no module.toml
    return tmp_path


def test_finds_every_module_in_order(root):
    found = M.discover(root)
    assert [m.key for m in found.modules] == ["zpiezo", "kim", "camera"]
    assert found.problems == []
    kim = found.get("kim")
    assert (kim.cmd, kim.pub) == (5567, 5568)
    assert kim.icon.name == "icon.svg" and kim.can_start and kim.has_gui


def test_headless_module_has_no_gui(root):
    z = M.discover(root).get("zpiezo")
    assert z.can_start and not z.has_gui


def test_a_broken_manifest_is_reported_not_fatal(root):
    bad = root / "bad-control"
    bad.mkdir()
    (bad / "module.toml").write_text("[module]\nkey = 'x y'\n")
    found = M.discover(root)
    assert len(found.modules) == 3
    assert any("bad-control" in p and "key" in p for p in found.problems)


def test_missing_script_is_reported(root):
    d = make_module(root, "lockin-control", "lockin", 5569)
    (d / "scripts" / "run_gui.py").unlink()
    found = M.discover(root)
    assert found.get("lockin") is None
    assert any("run_gui.py" in p for p in found.problems)


def test_duplicate_key_keeps_the_first(root):
    make_module(root, "zz-kim-copy", "kim", 5600)
    found = M.discover(root)
    assert [m.cmd for m in found.modules if m.key == "kim"] == [5567]
    assert any("already used" in p for p in found.problems)


def test_port_override_and_reset(root):
    M.set_ports("kim", 6000, None, root)
    kim = M.discover(root).get("kim")
    assert (kim.cmd, kim.pub) == (6000, 6001) and kim.ports_overridden
    M.set_ports("kim", None, None, root)
    kim = M.discover(root).get("kim")
    assert (kim.cmd, kim.pub) == (5567, 5568) and not kim.ports_overridden


def test_a_plain_preference_persists_beside_the_module_settings(root):
    """Applications keep a few non-module choices here (the data directory) so
    they survive a restart without inventing a second settings file."""
    assert M.get_setting("data_dir", "fallback", root) == "fallback"
    M.set_setting("data_dir", r"D:\measurements", root)
    assert M.get_setting("data_dir", root=root) == r"D:\measurements"

    # and it must not disturb what else is in the file
    M.set_real("kim", True, root)
    assert M.get_setting("data_dir", root=root) == r"D:\measurements"
    assert M.discover(root).get("kim").real

    M.set_setting("data_dir", None, root)
    assert M.get_setting("data_dir", root=root) is None


def test_real_flag_persists(root):
    M.set_real("kim", True, root)
    found = M.discover(root)
    assert found.get("kim").real and not found.get("camera").real
    assert M.service_args(found.get("kim"))[-1] == "--real"


def test_port_conflict_is_reported(root):
    M.set_ports("kim", 5563, 5564, root)       # camera's ports
    assert any("5563" in p for p in M.discover(root).problems)


def test_remote_add_borrows_the_local_twin_and_remove(root):
    rid = M.add_remote("192.168.1.20", 5567, 5568, "kim", root=root)
    assert rid == "kim@192.168.1.20:5567"
    found = M.discover(root)
    r = found.get(rid)
    assert r.remote and not r.can_start           # never started from here
    assert r.has_gui and r.icon is not None       # uses this PC's copy of kim
    assert r.name == "Kim"                        # the local twin's name
    assert M.gui_args(r, connect=True)[:2] == ["--connect", "192.168.1.20"]
    # same ports on ANOTHER host are not a conflict
    assert found.problems == []
    with pytest.raises(ValueError):
        M.add_remote("192.168.1.20", 5567, 5568, "kim", root=root)
    assert M.remove_remote(rid, root) is True
    assert M.discover(root).get(rid) is None
    assert M.remove_remote(rid, root) is False


def test_slugs_are_unique_and_safe(root):
    M.add_remote("192.168.1.20", 5567, 5568, "kim", root=root)
    M.add_remote("192.168.1.20", 6567, 6568, "kim", root=root)
    slugs = [m.slug for m in M.discover(root).modules]
    assert "kim" in slugs and "kim_192_168_1_20" in slugs and "kim_192_168_1_20_6567" in slugs
    assert len(slugs) == len(set(slugs))
    assert all(s.replace("_", "").isalnum() for s in slugs)


def test_remote_of_a_type_not_installed_here(root):
    rid = M.add_remote("lab2", 5571, 5572, "vna", name="VNA", root=root)
    r = M.discover(root).get(rid)
    assert r.name == "VNA" and not r.has_gui and r.icon is None


def test_start_order_respects_dependencies_inside_the_set(root):
    found = M.discover(root)
    cam, kim = found.get("camera"), found.get("kim")
    assert [m.key for m in M.start_order([cam, kim])] == ["kim", "camera"]
    # piezo is not in the set, so the camera does not wait for it
    assert [m.key for m in M.start_order([cam])] == ["camera"]


def test_start_order_survives_a_cycle():
    a = M.ModuleSpec(id="a", key="a", name="A", start_after=["b"])
    b = M.ModuleSpec(id="b", key="b", name="B", start_after=["a"])
    assert {m.key for m in M.start_order([a, b])} == {"a", "b"}


def test_bad_local_file_does_not_break_discovery(root):
    (root / M.LOCAL_FILE).write_text("{ not json")
    assert len(M.discover(root).modules) == 3


def test_local_file_with_bom_is_read(root):
    (root / M.LOCAL_FILE).write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"modules": {"kim": {"real": True}}}).encode())
    assert M.discover(root).get("kim").real


def test_endpoints_json_lists_local_by_key_remote_by_id(root):
    M.add_remote("lab2", 5567, 5568, "kim", root=root)
    table = json.loads(M.endpoints_json(M.discover(root).modules))
    assert table["kim"] == ["localhost", 5567, 5568]
    assert table["kim@lab2:5567"] == ["lab2", 5567, 5568]


def test_default_root_is_the_suite_folder(monkeypatch):
    monkeypatch.delenv("AALTOFLOW_ROOT", raising=False)
    monkeypatch.delenv("TRMOKE_ROOT", raising=False)
    assert (M.default_root() / "suite-common").is_dir()


def test_the_real_suite_discovers_cleanly(monkeypatch):
    """Every module.toml actually committed must parse, with no conflicts at
    their default ports. This is the test that fails when someone adds a module
    with a typo or a port that is already taken."""
    monkeypatch.delenv("AALTOFLOW_ROOT", raising=False)
    monkeypatch.delenv("TRMOKE_ROOT", raising=False)
    root = M.default_root()
    local, problems = M.discover_local(root)
    assert problems == []
    assert M.port_conflicts(local) == []
    assert {"clMag", "smb", "stage", "piezo", "zpiezo", "kim", "camera", "hf2"} <= {
        m.key for m in local}


def test_a_named_setup_leads_every_title(root):
    """AaltoFlow is the product; a PC may name the setup it drives (asked for by
    the installer) and every window then says which rig it belongs to."""
    assert M.setup_name(root) == ""
    assert M.title("Mission Control", root) == "AaltoFlow · Mission Control"
    M.set_setup_name("  TR-MOKE ", root)
    assert M.setup_name(root) == "TR-MOKE"
    assert M.title("Mission Control", root) == "TR-MOKE · Mission Control"
    assert M.title(root=root) == "TR-MOKE"
    M.set_setup_name("", root)                      # blank = back to the product name
    assert "setup_name" not in M.load_local(root).get("settings", {})


def test_the_pre_rename_environment_names_still_work(monkeypatch, tmp_path):
    monkeypatch.delenv("AALTOFLOW_ROOT", raising=False)
    monkeypatch.setenv("TRMOKE_ROOT", str(tmp_path))
    assert M.default_root() == tmp_path
    monkeypatch.setenv("AALTOFLOW_ROOT", str(tmp_path / "new"))
    assert M.default_root() == tmp_path / "new"      # the new name wins
