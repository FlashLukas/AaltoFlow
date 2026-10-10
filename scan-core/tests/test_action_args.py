"""A routine step's ACTION ARGUMENTS ("Advanced" options), 2026-10-10.

Lukas: the camera's AF position and the autofocus's settings are "advanced
settings in the procedure called in before/after/throughout scan". So a step
that runs an action may carry arguments -- {action: X, args: {...}} -- built
in the Scan Builder from the action's describe `args` list (any module's,
not only the camera's), saved in the recipe, sent with the verb, carried
through .yaml and .nc, and shown as tags on the step. Anything the step does
not set is not sent: the module uses its own setting.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run                 # noqa: E402
from scan_core.hooks import action_arg_problems, args_text, routine_steps   # noqa: E402

zmq = pytest.importorskip("zmq")

ARGS = {"ix": 2, "routine": "one_way", "go_back": False}


def _recipe(hooks):
    return Recipe(axes=[{"param": "field", "type": "linear", "start": 0, "stop": 1,
                         "num": 2}], detectors=["lockin_r"], hooks=hooks)


# --------------------------------------------------------------------------- #
# the data: routine_steps, validation, the engine
# --------------------------------------------------------------------------- #
def test_routine_steps_carries_the_arguments_in_both_spellings():
    assert routine_steps({"action": "X", "args": {"a": 1}}) == [("action", "X", {"a": 1})]
    assert routine_steps({"steps": [{"action": "X", "args": {"a": 1}}, {"action": "Y"}]}) \
        == [("action", "X", {"a": 1}), ("action", "Y")]
    assert routine_steps({"action": "X", "args": {}}) == [("action", "X")]
    with pytest.raises(ValueError, match="belong to an action"):
        routine_steps({"steps": [{"args": {"a": 1}}]})
    with pytest.raises(ValueError, match="must map"):
        routine_steps({"action": "X", "args": [1, 2]})


def test_the_values_are_checked_against_what_the_module_declared():
    reg = build_sim_registry()
    act = reg.get_action("sim_focus_at")
    assert action_arg_problems(act, ARGS) == []
    for bad, msg in (({"ix": 9}, "outside"), ({"ix": 1.5}, "whole number"),
                     ({"routine": "zigzag"}, "not one of"), ({"go_back": 1}, "true or false"),
                     ({"colour": "red"}, "no argument called")):
        probs = action_arg_problems(act, bad)
        assert probs and msg in probs[0], (bad, probs)
    # an action without arguments refuses any
    assert "takes no arguments" in action_arg_problems(reg.get_action("sim_autofocus"),
                                                        {"x": 1})[0]
    # and recipe.validate names the routine
    hooks = [{"when": "before_scan", "action": "call",
              "args": {"action": "sim_focus_at", "args": {"ix": 9}}}]
    errs = _recipe(hooks).validate(reg)
    assert len(errs) == 1 and "outside" in errs[0], errs


def test_the_engine_passes_the_arguments_to_the_action():
    reg = build_sim_registry()
    hooks = [{"when": "before_scan", "action": "call",
              "args": {"steps": [{"action": "sim_focus_at", "args": ARGS}]}}]
    r = _recipe(hooks)
    assert r.validate(reg) == []
    log = []
    run(r, reg, on_log=log.append)
    assert reg._state.last_focus_at == ARGS
    assert any("ix=2, routine=one_way, go_back=off" in m for m in log), log


def test_args_text():
    assert args_text({"ix": 2, "x_um": 1.5, "go_back": True}) == "ix=2, x_um=1.5, go_back=on"


# --------------------------------------------------------------------------- #
# over the wire: a fake service records what it received
# --------------------------------------------------------------------------- #
class FakeCamera:
    """A module with ONE waitable action that takes arguments (no defaults
    except go_back), like the camera's autofocus_at_position."""

    def __init__(self, port):
        self.cmd_port, self.pub_port = port, port + 1
        self.commands: list[dict] = []
        self._id = 0
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        self.manifest = {"schema": 1, "module": "camera", "revision": 1, "parameters": [
            {"id": "autofocus_at_position", "label": "Find focus at AF position",
             "kind": "action", "type": "action",
             "args": [{"name": "ix", "label": "AF point index X", "type": "int",
                       "min": 0, "max": 4},
                      {"name": "routine", "label": "AF routine", "type": "enum",
                       "options": ["sweep", "one_way"]},
                      {"name": "go_back", "label": "Return", "type": "bool",
                       "default": True}],
             "wait": {"target_key": "af_id",
                      "ready": {"policy": "adopt_then_flag", "setpoint_key": "af_id",
                                "flag_key": "af_running", "invert": True},
                      "check": {"key": "af_error", "equals": "OK"}, "timeout_s": 5}},
        ]}

    def start(self):
        for fn in (self._serve, self._publish):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.2)

    def _status(self):
        return {"af_id": self._id, "af_running": False, "af_error": "OK"}

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                msg = rep.recv_json()
                cmd = msg.get("cmd")
                if cmd == "autofocus_at_position":
                    self.commands.append({k: v for k, v in msg.items() if k != "client"})
                    self._id += 1
                    rep.send_json({"ok": True, "af_id": self._id})
                elif cmd == "status":
                    rep.send_json({"ok": True, "status": self._status()})
                elif cmd == "describe":
                    rep.send_json({"ok": True, "describe": self.manifest})
                else:
                    rep.send_json({"ok": False, "error": f"unknown {cmd}"})
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.03)
        pub.close(0)


@pytest.fixture
def camera_reg():
    from scan_core.instrument import Instrument
    from scan_core.manifest import register_manifest
    svc = FakeCamera(15976).start()
    inst = Instrument("camera", host="127.0.0.1", cmd_port=15976)
    time.sleep(0.2)
    reg = build_sim_registry()
    register_manifest(reg, inst, inst.command("describe")["describe"], prefix=True)
    yield svc, reg
    inst.close()
    svc.stop()


def test_the_describe_args_reach_the_registry_action(camera_reg):
    _svc, reg = camera_reg
    act = reg.get_action("camera.autofocus_at_position")
    assert [a["name"] for a in act.arg_specs] == ["ix", "routine", "go_back"]


def test_the_verb_receives_exactly_the_step_arguments(camera_reg):
    svc, reg = camera_reg
    hooks = [{"when": "before_scan", "action": "call", "args": {
        "action": "camera.autofocus_at_position", "args": {"ix": 3, "routine": "one_way"}}},
             {"when": "after_scan", "action": "call", "args": {
                 "action": "camera.autofocus_at_position"}}]
    r = _recipe(hooks)
    assert r.validate(reg) == []
    run(r, reg)
    # the step's values, plus the declared default of what it left out
    # (go_back) -- and NOTHING for ix / routine when the step sets none
    assert svc.commands == [
        {"cmd": "autofocus_at_position", "go_back": True, "ix": 3, "routine": "one_way"},
        {"cmd": "autofocus_at_position", "go_back": True}]


# --------------------------------------------------------------------------- #
# the Scan Builder: the Advanced expander on an action step
# --------------------------------------------------------------------------- #
@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    yield win
    win.close()


def test_the_expander_is_built_from_the_declared_args(builder):
    from PySide6 import QtWidgets
    sec = builder.routines["before_scan"]
    plain = sec.add_action("sim_autofocus")
    assert plain.adv_btn is None                       # no arguments: no gear
    row = sec.add_action("sim_focus_at")
    assert row.adv_btn is not None and not row.advanced_open()
    row.adv_btn.click()
    assert row.advanced_open()
    lines = row.args_panel.lines
    assert list(lines) == ["ix", "iy", "x_um", "go_back", "routine"]
    tick, ed, _ = lines["ix"]
    assert isinstance(ed, QtWidgets.QSpinBox) and (ed.minimum(), ed.maximum()) == (0, 4)
    assert isinstance(lines["x_um"][1], QtWidgets.QDoubleSpinBox)
    assert lines["x_um"][1].suffix() == " um"
    assert isinstance(lines["routine"][1], QtWidgets.QComboBox)
    assert [lines["routine"][1].itemText(i) for i in range(2)] == ["sweep", "one_way"]
    assert isinstance(lines["go_back"][1], QtWidgets.QCheckBox)
    assert lines["go_back"][1].isChecked()             # the declared default
    # nothing ticked: nothing sent, no tags, the step writes no args
    assert row.args() == {} and row.to_step() == {"action": "sim_focus_at"}
    assert not row.tags.isVisibleTo(row)


def test_ticked_values_are_saved_tagged_and_reloaded(builder, tmp_path):
    sec = builder.routines["before_scan"]
    row = sec.add_action("sim_focus_at")
    ix_tick, ix_ed, _ = row.args_panel.lines["ix"]
    ix_tick.setChecked(True); ix_ed.setValue(2)
    r_tick, r_ed, _ = row.args_panel.lines["routine"]
    r_tick.setChecked(True); r_ed.setCurrentText("one_way")
    g_tick, g_ed, _ = row.args_panel.lines["go_back"]
    g_tick.setChecked(True); g_ed.setChecked(False)
    assert row.args() == ARGS
    # tags on the collapsed step, one per argument set
    tags = [row.tags_box.itemAt(i).widget().text() for i in range(row.tags_box.count() - 1)]
    assert tags == ["ix=2", "go_back=off", "routine=one_way"]
    hooks = builder.build_recipe().hooks
    assert hooks == [{"when": "before_scan", "action": "call",
                      "args": {"action": "sim_focus_at", "args": ARGS}}]
    assert "sim_focus_at (ix=2, go_back=off, routine=one_way)" in sec.describe()
    # .yaml round trip, then back into the card
    builder.build_recipe().save(tmp_path / "x.yaml")
    back = Recipe.load(tmp_path / "x.yaml")
    assert back.hooks == hooks
    assert builder.load_recipe(back) == []
    row2 = builder.routines["before_scan"].steps[0]
    assert row2.args() == ARGS
    assert builder.build_recipe().hooks == hooks


def test_args_in_the_ordered_form_and_in_a_throughout_routine(builder):
    hooks = [
        {"when": "before_scan", "action": "call", "args": {"steps": [
            {"action": "sim_focus_at", "args": {"x_um": -12.5}},
            {"action": "sim_autofocus"}]}},
        {"when": "each_sweep", "axis": "field", "edge": "start", "every": 1,
         "on_error": "continue", "action": "call",
         "args": {"action": "sim_focus_at", "args": {"iy": 1}}},
    ]
    r = _recipe(hooks)
    assert r.validate(builder.registry) == []
    assert builder.load_recipe(r) == []
    assert builder.build_recipe().hooks == hooks


def test_args_survive_the_netcdf_file(tmp_path):
    """The recipe (with the step's args) is stored in every .nc; loading the
    definition from the file (what "Load" does with a .nc) brings them back."""
    from scan_core.scan_queue import recipe_from_file
    reg = build_sim_registry()
    hooks = [{"when": "before_scan", "action": "call",
              "args": {"action": "sim_focus_at", "args": ARGS}}]
    path = tmp_path / "scan.nc"
    run(_recipe(hooks), reg).to_netcdf(path)
    back = recipe_from_file(path)
    back = back[0] if isinstance(back, tuple) else back
    assert back.hooks == hooks
