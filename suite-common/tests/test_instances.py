"""Module INSTANCES: a second copy of ONE local module on this PC.

2026-10-11 (Lukas): the scope module runs once for the Siglent and once, with
`--driver dwf`, for an Analog Discovery 2 -- side by side, each with its own
ports, its own card in Mission Control and its own parameter prefix in a scan.
The copies are this PC's choice: suite_local.json, modules.<key>.instances.
"""

import json
from pathlib import Path

import pytest

from suite_common import modules as M
from test_modules import make_module


@pytest.fixture
def root(tmp_path):
    make_module(tmp_path, "kim-control", "kim", 5567, order=60)
    make_module(tmp_path, "camera-control", "camera", 5563, order=70,
                start_after=["kim", "piezo"])
    make_module(tmp_path, "zpiezo-control", "zpiezo", 5565, gui=False, order=50)
    return tmp_path


def test_no_instances_means_discovery_is_unchanged(root):
    found = M.discover(root)
    assert [m.id for m in found.modules] == ["zpiezo", "kim", "camera"]
    assert all(not m.instance and m.extra_args == [] for m in found.modules)
    assert found.problems == []


def test_an_instance_is_its_own_module_with_its_own_ports_and_args(root):
    iid = M.add_instance("kim", "b", 6100, args=["--driver", "dwf"], root=root)
    assert iid == "kim#b" and M.split_id(iid) == ("kim", "b")
    found = M.discover(root)
    assert found.problems == []
    ids = [m.id for m in found.modules]
    assert ids.index("kim#b") == ids.index("kim") + 1        # right after its module
    base, inst = found.get("kim"), found.get("kim#b")
    assert found.by_key("kim") is base                        # never the instance
    assert inst.instance == "b" and inst.key == "kim" and inst.name == "Kim (b)"
    assert (inst.cmd, inst.pub) == (6100, 6101) and not inst.ports_overridden
    assert (base.cmd, base.pub) == (5567, 5568)               # the module is untouched
    assert inst.can_start and inst.has_gui and inst.dir == base.dir
    assert inst.slug == "kim_b" and base.slug == "kim"
    assert M.service_args(inst) == ["--cmd-port", "6100", "--pub-port", "6101",
                                    "--driver", "dwf"]
    assert M.service_args(base) == ["--cmd-port", "5567", "--pub-port", "5568"]
    assert M.gui_args(inst, connect=True) == ["--connect", "localhost",
                                              "--cmd-port", "6100", "--pub-port", "6101"]


def test_an_instance_has_its_own_real_flag_address_and_settings_file(root):
    toml = root / "kim-control" / "module.toml"
    toml.write_text(toml.read_text() + '\n[hardware]\naddress_arg = "--visa"\nbus = "visa"\n',
                    encoding="utf-8")
    M.add_instance("kim", "two", 6100, 6105, config="kim-two.ini", root=root)
    M.set_real("kim#two", True, root)
    M.set_address("kim#two", "USB0::B::INSTR", root)
    M.set_ports("kim#two", 6200, 6201, root)
    found = M.discover(root)
    base, inst = found.get("kim"), found.get("kim#two")
    assert inst.real and not base.real
    assert inst.address == "USB0::B::INSTR" and base.address == ""
    args = M.service_args(inst)
    assert args[:5] == ["--cmd-port", "6200", "--pub-port", "6201", "--real"]
    assert args[5:7] == ["--visa", "USB0::B::INSTR"]
    assert args[7] == "--config" and Path(args[8]) == base.dir / "kim-two.ini"
    with pytest.raises(ValueError):          # an instance has no default to reset to
        M.set_ports("kim#two", None, None, root)
    with pytest.raises(ValueError):
        M.set_real("kim#nope", True, root)


def test_instance_names_and_args_are_checked(root):
    for bad in ("", "has space", "dash-ed", "a#b"):
        with pytest.raises(ValueError):
            M.add_instance("kim", bad, 6100, root=root)
    for flag in ("--cmd-port", "--real", "--config=x.ini"):
        with pytest.raises(ValueError, match="launcher"):
            M.add_instance("kim", "b", 6100, args=[flag], root=root)
    with pytest.raises(ValueError):
        M.add_instance("kim", "b", 6100, 6100, root=root)
    M.add_instance("kim", "b", 6100, root=root)
    with pytest.raises(ValueError, match="already"):
        M.add_instance("kim", "B", 6300, root=root)           # names are case-blind
    assert M.parse_args_text('--driver dwf --name "two words"') == \
        ["--driver", "dwf", "--name", "two words"]
    assert M.parse_args_text(M.args_text(["--name", "two words"])) == ["--name", "two words"]
    with pytest.raises(ValueError):
        M.parse_args_text('--name "unclosed')


def test_remove_instance(root):
    iid = M.add_instance("kim", "b", 6100, root=root)
    M.set_real("kim", True, root)                             # the module's own entry stays
    assert M.remove_instance(iid, root) is True
    assert M.discover(root).get(iid) is None
    assert M.discover(root).get("kim").real
    assert "instances" not in M.load_local(root)["modules"]["kim"]
    assert M.remove_instance(iid, root) is False
    assert M.remove_instance("kim", root) is False            # never the module itself


def test_port_conflicts_across_instances_are_reported(root):
    M.add_instance("kim", "b", 5563, root=root)               # camera's ports
    probs = M.discover(root).problems
    assert any("5563" in p and "kim#b" in p and "camera" in p for p in probs)
    M.remove_instance("kim#b", root)
    M.add_instance("kim", "b", 6100, root=root)
    M.add_instance("camera", "b", 6100, root=root)            # two instances clash
    assert any("6100" in p and "kim#b" in p and "camera#b" in p
               for p in M.discover(root).problems)


def test_bad_instance_entries_are_problems_not_crashes(root):
    data = {"modules": {
        "kim": {"instances": [{"name": "ok", "cmd": 6100},
                              {"name": "bad name", "cmd": 6200},
                              {"name": "noports"},
                              {"name": "ok", "cmd": 6300},          # twice
                              {"name": "flag", "cmd": 6400, "args": ["--real"]},
                              "not a dict"]},
        "vna": {"instances": [{"name": "x", "cmd": 6500}]}}}       # not installed
    (root / M.LOCAL_FILE).write_text(json.dumps(data))
    found = M.discover(root)
    assert [m.id for m in found.modules if m.instance] == ["kim#ok"]
    assert sum("instance" in p for p in found.problems) == 6


def test_endpoints_json_lists_an_instance_by_its_id(root):
    M.add_instance("kim", "b", 6100, root=root)
    table = json.loads(M.endpoints_json(M.discover(root).modules))
    assert table["kim"] == ["localhost", 5567, 5568]          # the module keeps its key
    assert table["kim#b"] == ["localhost", 6100, 6101]


def test_instance_slug_never_takes_another_modules_key(tmp_path):
    make_module(tmp_path, "kim-control", "kim", 5567)
    make_module(tmp_path, "kimb-control", "kim_b", 5600)     # a module whose key IS kim_b
    M.add_instance("kim", "b", 6100, root=tmp_path)
    M.add_remote("b", 5567, 5568, "kim", root=tmp_path)       # remote host "b": kim_b too
    slugs = {m.id: m.slug for m in M.discover(tmp_path).modules}
    assert slugs["kim_b"] == "kim_b" and slugs["kim#b"] == "kim_b_6100"
    assert len(set(slugs.values())) == len(slugs)


def test_start_order_with_an_instance(root):
    M.add_instance("kim", "b", 6100, root=root)
    found = M.discover(root)
    order = [m.id for m in M.start_order([found.get("camera"), found.get("kim#b"),
                                          found.get("kim")])]
    assert order.index("camera") > order.index("kim#b")
    assert order.index("camera") > order.index("kim")


def test_the_catalog_never_lists_an_instance(root):
    from suite_common.catalog import build_catalog
    M.add_instance("kim", "b", 6100, root=root)
    keys = [e["key"] for e in build_catalog(M.discover(root).modules)["modules"]]
    assert keys.count("kim") == 1
