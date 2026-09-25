"""Routine actions learn where the measurement is written.

A module's action can save something of its own next to the data (the camera's
picture and pattern). Only scan-core knows the data file, so it fills
{data_dir}, {data_stem} and {moment} in the action's text arguments.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, run                                     # noqa: E402
from scan_core.manifest import fill_placeholders                     # noqa: E402
from scan_core.registry import Action, Gettable, Registry, Settable  # noqa: E402


def test_placeholders_are_filled_and_unknown_ones_become_empty():
    ctx = {"data_dir": r"D:\data\2026-09-25", "data_stem": "134501_map", "moment": "before"}
    got = fill_placeholders({"folder": "{data_dir}", "name": "{data_stem}_{moment}_camera",
                             "n": 3, "odd": "{nope}x", "brace": "a{b"}, ctx)
    assert got == {"folder": r"D:\data\2026-09-25", "name": "134501_map_before_camera",
                   "n": 3, "odd": "x", "brace": "a{b"}
    # outside a scan: everything empty, the module uses its own folder
    assert fill_placeholders({"folder": "{data_dir}"}, None) == {"folder": ""}


def _registry(seen):
    state = {"x": 0.0}
    reg = Registry()
    reg.add(Settable("x", "x", "", (-10, 10), lambda v: state.update(x=v), lambda: state["x"]))
    reg.add(Gettable("d", "d", "", lambda: state["x"]))
    reg.add_action(Action("pic", "picture", lambda context=None: seen.append(context)))
    reg.add_action(Action("plain", "no context", lambda: seen.append("plain")))
    return reg


def test_the_action_gets_the_data_file_and_the_moment():
    seen = []
    hooks = [{"when": "before_scan", "action": "call", "args": {"action": "pic"}},
             {"when": "every_n_points", "n": 2, "action": "call", "args": {"action": "pic"}},
             {"when": "after_scan", "action": "call", "args": {"action": "pic"}},
             {"when": "before_scan", "action": "call", "args": {"action": "plain"}}]
    run(Recipe(axes=[{"type": "array", "param": "x", "values": [0, 1, 2]}],
               detectors=["d"], hooks=hooks),
        _registry(seen), created_iso="t",
        data_path=Path(r"D:\data\2026-09-25\134501_map.nc"))
    ctxs = [c for c in seen if isinstance(c, dict)]
    assert [c["moment"] for c in ctxs] == ["before", "p00001", "p00003", "after"]
    assert all(c["data_stem"] == "134501_map" for c in ctxs)
    assert all(c["data_dir"].endswith("2026-09-25") for c in ctxs)
    assert "plain" in seen                        # an action without context still runs


def test_an_unsaved_run_gives_empty_places():
    seen = []
    run(Recipe(axes=[{"type": "array", "param": "x", "values": [0]}], detectors=["d"],
               hooks=[{"when": "before_scan", "action": "call", "args": {"action": "pic"}}]),
        _registry(seen), created_iso="t")
    assert seen == [{"data_dir": "", "data_stem": "", "moment": "before"}]
