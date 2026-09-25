"""ROUTINES: what a scan does once before it starts and once after it ends.

The case that asked for it (VNA-FMR, from the old RotSampleInVNA program): go
to a far-off REFERENCE field, wait until stable, take a reference trace; sweep
the field and record u = (S - S_ref)/S_ref; finally field -> 0.

In a recipe that is two `call` hooks:

    {when: before_scan, action: call, args: {set: {field: 190}, action: vna_reference}}
    {when: after_scan,  action: call, args: {set: {field: 0}}}

What these tests pin down: WHEN the routines run relative to the conditions and
the points; that after_scan also runs after an Abort (and not after an error);
that a routine does not leave the scan at the reference field (the restore); that
validation and loading name what is missing; and that the Scan Builder's
ROUTINES card round-trips through .yaml and .nc without dropping other hooks.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run                  # noqa: E402
from scan_core.data import as_complex                                  # noqa: E402
from scan_core.errors import RoutineError, ScanAborted                 # noqa: E402
from scan_core.registry import Action, Gettable, Registry, Settable    # noqa: E402

RECIPES = Path(__file__).resolve().parent.parent / "recipes"


# ─────────────────────── a registry that writes a diary ───────────────────────

def _diary_registry():
    """Settables a, b, c, o and one action X, every call written to `events`.

    The detector reads the CURRENT value of b, so a test can check that each
    point was measured where its coordinate says.
    """
    events, state = [], {"a": 0.0, "b": 0.0, "c": 0.0, "o": 0.0}
    reg = Registry()

    def settable(pid):
        def set_fn(v, _p=pid):
            state[_p] = v
            events.append(("set", _p, v))
        reg.add(Settable(pid, pid.upper(), "mT", (-500, 500), set_fn,
                         lambda _p=pid: state[_p]))

    for pid in ("a", "b", "c", "o"):
        settable(pid)

    def read_b():
        events.append(("read", state["b"]))
        return state["b"]

    reg.add(Gettable("b_now", "b as measured", "mT", read_b))
    reg.add_action(Action("X", "Action X", lambda: events.append(("action", "X"))))
    return reg, events, state


def _hook(when, set=None, action=None, **extra):
    args = {}
    if set is not None:
        args["set"] = set
    if action is not None:
        args["action"] = action
    return {"when": when, "action": "call", "args": args, **extra}


def _hook_steps(when, steps, **extra):
    """A routine in the ORDERED form (2026-09-25): {steps: [...]}."""
    return {"when": when, "action": "call", "args": {"steps": steps}, **extra}


# ────────────────────────────── the registry API ──────────────────────────────

def test_actions_are_kept_apart_from_parameters():
    """A button is not a sweepable value: an action must not appear where the
    palette and the validator look for settables and detectors."""
    reg, _, _ = _diary_registry()
    assert [a.id for a in reg.actions()] == ["X"]
    assert reg.get_action("X").label == "Action X"
    assert reg.get("X") is None
    assert reg.get_action("nope") is None
    assert "X" not in {p.id for p in reg.settables() + reg.gettables()}


# ───────────────────────────────── the engine ─────────────────────────────────

def test_before_scan_runs_after_the_conditions_and_before_the_first_point():
    reg, events, _ = _diary_registry()
    recipe = Recipe(fixed={"a": 1.0},
                    axes=[{"type": "array", "param": "b", "values": [10, 20, 30]}],
                    detectors=["b_now"],
                    hooks=[_hook("before_scan", {"c": 5.0}, "X"),
                           _hook("after_scan", {"c": 0.0})])
    run(recipe, reg, created_iso="t")
    assert events == [
        ("set", "a", 1.0),                       # the condition
        ("set", "c", 5.0), ("action", "X"),      # before_scan: sets, THEN the action
        ("set", "b", 10.0), ("read", 10.0),
        ("set", "b", 20.0), ("read", 20.0),
        ("set", "b", 30.0), ("read", 30.0),
        ("set", "c", 0.0),                       # after_scan, after the last read
    ]


def test_the_run_log_says_what_the_routines_did():
    reg, _, _ = _diary_registry()
    logs = []
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [0]}],
                    detectors=["b_now"],
                    hooks=[_hook("before_scan", {"c": 5.0}, "X")])
    run(recipe, reg, created_iso="t", on_log=logs.append)
    assert "before_scan: set c = 5 mT done" in logs
    assert "before_scan: run X done" in logs
    assert all(m.isascii() for m in logs)          # gotcha 14: printable anywhere


def test_hooks_see_current_and_recipe():
    reg, _, _ = _diary_registry()
    seen = {}
    from scan_core import hooks

    @hooks.action("_peek")
    def _peek(ctx, **kw):
        seen[ctx["moment"]] = (dict(ctx["current"]), ctx["recipe"].name)

    try:
        recipe = Recipe(name="peek", fixed={"a": 2.0},
                        axes=[{"type": "array", "param": "b", "values": [1, 2]}],
                        detectors=["b_now"],
                        hooks=[{"when": "before_scan", "action": "_peek"},
                               {"when": "after_scan", "action": "_peek"}])
        run(recipe, reg, created_iso="t")
    finally:
        hooks.ACTIONS.pop("_peek", None)
    assert seen["before_scan"] == ({"a": 2.0}, "peek")
    assert seen["after_scan"] == ({"a": 2.0, "b": 2.0}, "peek")


def test_after_scan_runs_after_an_abort():
    """Stopping a scan early is exactly when you want the magnet put back."""
    reg, events, state = _diary_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1, 2, 3, 4]}],
                    detectors=["b_now"],
                    hooks=[_hook("after_scan", {"c": 0.0})])
    state["c"] = 99.0
    run(recipe, reg, created_iso="t",
        should_abort=lambda: len([e for e in events if e[0] == "read"]) >= 2)
    assert events[-1] == ("set", "c", 0.0)
    assert len([e for e in events if e[0] == "read"]) == 2


def test_after_scan_runs_when_the_abort_lands_inside_a_settle_wait():
    """ScanAborted from deep inside a set (Instrument.wait_until) is an abort too:
    after_scan runs, then the abort carries on up to the caller."""
    reg, events, _ = _diary_registry()

    def stuck(v):
        raise ScanAborted("aborted while waiting for b")

    reg.add(Settable("stuck", "stuck", "", (-10, 10), stuck, lambda: 0.0))
    recipe = Recipe(axes=[{"type": "array", "param": "stuck", "values": [1]}],
                    detectors=["b_now"], hooks=[_hook("after_scan", {"c": 0.0})])
    with pytest.raises(ScanAborted):
        run(recipe, reg, created_iso="t")
    assert events[-1] == ("set", "c", 0.0)


def test_after_scan_after_an_abort_sends_every_command_but_does_not_wait():
    """Abort is still pressed while the after-scan routine runs, so an instrument
    wait inside it raises at once. The routine must carry on to its next step
    (the field still goes to 0), not stop at the first one."""
    reg, events, _ = _diary_registry()

    def impatient(v):
        events.append(("set", "slow", v))
        raise ScanAborted("aborted while waiting")      # what wait_until does

    reg.add(Settable("slow", "slow", "", (-10, 10), impatient, lambda: 0.0))
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1, 2]}],
                    detectors=["b_now"],
                    hooks=[_hook("after_scan", {"slow": 0.0, "c": 0.0}, "X")])
    logs = []
    run(recipe, reg, created_iso="t", should_abort=lambda: True, on_log=logs.append)
    assert events[-3:] == [("set", "slow", 0.0), ("set", "c", 0.0), ("action", "X")]
    assert any("not waited for (aborted)" in m for m in logs)


def test_after_scan_does_not_run_after_an_error():
    """An exception means something is broken; drive nothing more."""
    reg, events, _ = _diary_registry()

    def broken():
        raise RuntimeError("the detector fell over")

    reg.add(Gettable("broken", "broken", "", broken))
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1]}],
                    detectors=["broken"], hooks=[_hook("after_scan", {"c": 0.0})])
    with pytest.raises(RuntimeError, match="fell over"):
        run(recipe, reg, created_iso="t")
    assert ("set", "c", 0.0) not in events


def test_a_failing_after_scan_routine_keeps_the_measured_data():
    reg, _, _ = _diary_registry()
    reg.add_action(Action("boom", "boom", lambda: (_ for _ in ()).throw(TimeoutError("never stable"))))
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1, 2]}],
                    detectors=["b_now"], hooks=[_hook("after_scan", action="boom")])
    with pytest.raises(RoutineError, match="never stable") as exc:
        run(recipe, reg, created_iso="t")
    assert exc.value.dataset is not None
    assert list(exc.value.dataset["b_now"].values) == [1.0, 2.0]


def test_an_axis_hook_fires_only_for_its_own_axis():
    """{when: before_axis, axis: o} used to fire on EVERY axis change -- i.e. at
    every inner point. Harmless for the autofocus stub, not for a routine."""
    reg, events, _ = _diary_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "o", "values": [0, 1]},
                          {"type": "array", "param": "b", "values": [0, 1, 2]}],
                    detectors=["b_now"],
                    hooks=[_hook("before_axis", action="X", axis="o")])
    run(recipe, reg, created_iso="t")
    assert events.count(("action", "X")) == 2


# ────────────────────────────────── restore ───────────────────────────────────

def test_a_before_scan_routine_restores_a_condition_it_moved():
    """Reference at another angle, then back to the condition's angle."""
    reg, events, state = _diary_registry()
    recipe = Recipe(fixed={"a": 45.0},
                    axes=[{"type": "array", "param": "b", "values": [70, 0]}],
                    detectors=["b_now"],
                    hooks=[_hook("before_scan", {"b": 150.0, "a": 10.0}, "X")])
    run(recipe, reg, created_iso="t")
    i_x = events.index(("action", "X"))
    # the axis param is NOT restored before the scan (it is not set yet; the
    # first point sets it) -- the condition IS, before any point is read
    assert events[i_x + 1] == ("set", "a", 45.0)
    assert events[i_x + 2] == ("set", "b", 70.0)
    assert state["a"] == 45.0


def test_a_routine_that_sets_a_condition_to_its_own_value_does_not_re_set_it():
    reg, events, _ = _diary_registry()
    recipe = Recipe(fixed={"a": 45.0},
                    axes=[{"type": "array", "param": "b", "values": [1]}],
                    detectors=["b_now"],
                    hooks=[_hook("before_scan", {"a": 45.0}, "X")])
    run(recipe, reg, created_iso="t")
    assert events.count(("set", "a", 45.0)) == 2          # condition + routine, no restore


def test_after_scan_is_not_restored():
    """'field -> 0 at the end' must stay at 0."""
    reg, _, state = _diary_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1, 2]}],
                    detectors=["b_now"], hooks=[_hook("after_scan", {"b": 0.0})])
    run(recipe, reg, created_iso="t")
    assert state["b"] == 0.0


@pytest.mark.parametrize("zigzag", [False, True])
def test_a_mid_scan_routine_cannot_leave_the_inner_axis_elsewhere(zigzag):
    """A routine at the start of every outer pass moves the inner axis away.

    With zig-zag the inner INDEX does not change when the outer one does (the
    row turns round), so the engine does not re-set it -- without the restore
    the first point of every other row would be read at the routine's value
    under a coordinate that says otherwise."""
    reg, events, _ = _diary_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "o", "values": [0, 1, 2]},
                          {"type": "array", "param": "b", "values": [10, 20, 30]}],
                    detectors=["b_now"], zigzag=zigzag,
                    hooks=[_hook("before_axis", {"b": 400.0}, axis="o")])
    ds = run(recipe, reg, created_iso="t")
    measured = ds["b_now"].values
    expected = np.broadcast_to(ds["b"].values, measured.shape)
    assert np.array_equal(measured, expected)
    assert ("read", 400.0) not in events


# ───────────────────── several steps, in order (2026-09-25) ────────────────────

def _with_y(reg, events):
    reg.add_action(Action("Y", "Action Y", lambda: events.append(("action", "Y"))))
    return reg


def test_ordered_steps_run_exactly_in_the_order_written():
    """"Find focus, then save the pattern, then save a picture": several
    actions, and sets between them, run top to bottom."""
    reg, events, _ = _diary_registry()
    _with_y(reg, events)
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1]}],
                    detectors=["b_now"],
                    hooks=[_hook_steps("before_scan", [
                               {"action": "X"}, {"set": {"c": 5.0}},
                               {"action": "Y"}, {"action": "X"}]),
                           _hook_steps("after_scan", [
                               {"action": "Y"}, {"set": {"c": 0.0}}])])
    run(recipe, reg, created_iso="t")
    assert events == [
        ("action", "X"), ("set", "c", 5.0), ("action", "Y"), ("action", "X"),
        ("set", "b", 1.0), ("read", 1.0),
        ("action", "Y"), ("set", "c", 0.0)]


def test_the_restore_runs_once_after_the_last_step():
    """[field 190, reference, field 0] with the field a CONDITION of 50: the
    scan must end up at 50 (what the file says), with no 190 -> 50 ramp in the
    middle of the routine."""
    reg, events, state = _diary_registry()
    recipe = Recipe(fixed={"a": 50.0},
                    axes=[{"type": "array", "param": "b", "values": [1]}],
                    detectors=["b_now"],
                    hooks=[_hook_steps("before_scan", [
                        {"set": {"a": 190.0}}, {"action": "X"}, {"set": {"a": 0.0}}])])
    run(recipe, reg, created_iso="t")
    assert events[:5] == [("set", "a", 50.0),                 # the condition
                          ("set", "a", 190.0), ("action", "X"), ("set", "a", 0.0),
                          ("set", "a", 50.0)]                 # restored ONCE, at the end
    assert state["a"] == 50.0


def test_an_axis_set_by_a_before_scan_routine_is_left_to_the_first_point():
    """The same list on the AXIS: nothing to restore, the first point sets it."""
    reg, events, _ = _diary_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [70, 0]}],
                    detectors=["b_now"],
                    hooks=[_hook_steps("before_scan", [
                        {"set": {"b": 190.0}}, {"action": "X"}, {"set": {"b": 0.0}}])])
    run(recipe, reg, created_iso="t")
    assert events[:4] == [("set", "b", 190.0), ("action", "X"), ("set", "b", 0.0),
                          ("set", "b", 70.0)]


def test_a_mid_scan_routine_with_steps_puts_the_axis_back():
    reg, events, _ = _diary_registry()
    _with_y(reg, events)
    recipe = Recipe(axes=[{"type": "array", "param": "o", "values": [0, 1]},
                          {"type": "array", "param": "b", "values": [10, 20]}],
                    detectors=["b_now"],
                    hooks=[_hook_steps("each_sweep", [
                        {"set": {"b": 400.0}}, {"action": "X"},
                        {"set": {"b": 300.0}}, {"action": "Y"}], axis="b")])
    ds = run(recipe, reg, created_iso="t")
    assert np.array_equal(ds["b_now"].values, np.broadcast_to(ds["b"].values, (2, 2)))
    assert ("read", 400.0) not in events and ("read", 300.0) not in events
    assert [e for e in events if e[0] == "action"] == [("action", "X"), ("action", "Y")] * 2


def test_a_failed_step_is_carried_on_from_and_the_rest_still_runs():
    reg, events, _ = _diary_registry()

    def broken():
        raise RuntimeError("no peak")

    reg.add_action(Action("AF", "AF", broken))
    logs = []
    recipe = Recipe(axes=[{"type": "array", "param": "b", "values": [1, 2]}],
                    detectors=["b_now"],
                    hooks=[_hook_steps("every_n_points", [{"action": "AF"}, {"action": "X"}],
                                       n=1, on_error="continue")])
    run(recipe, reg, created_iso="t", on_log=logs.append)
    assert events.count(("action", "X")) == 2
    assert sum("FAILED (no peak)" in m for m in logs) == 2


def test_the_two_spellings_are_one_list_of_steps():
    from scan_core.hooks import routine_steps
    assert routine_steps({"set": {"a": 1, "b": 2}, "action": "X"}) == \
        routine_steps({"steps": [{"set": {"a": 1}}, {"set": {"b": 2}}, {"action": "X"}]}) == \
        [("set", "a", 1), ("set", "b", 2), ("action", "X")]
    assert routine_steps({}) == []


# ─────────────────────────────── validation ───────────────────────────────────

def _errs(hooks):
    reg, _, _ = _diary_registry()
    reg.add(Gettable("det", "det", "", lambda: 0.0))
    return Recipe(axes=[{"type": "array", "param": "b", "values": [1]}],
                  detectors=["b_now"], hooks=hooks).validate(reg)


def test_validate_accepts_a_good_routine():
    assert _errs([_hook("before_scan", {"a": 1.0}, "X"),
                  _hook("after_scan", {"b": 0.0})]) == []
    assert _errs([_hook_steps("before_scan", [
        {"set": {"a": 1.0}}, {"action": "X"}, {"set": {"a": 0.0}}, {"action": "X"}])]) == []


@pytest.mark.parametrize("hook, expected", [
    (_hook("before_scan", {"nope.field": 1.0}), "unknown parameter 'nope.field'"),
    (_hook("before_scan", {"det": 1.0}), "'det' is not settable"),
    (_hook("before_scan", {"a": 900.0}), "outside its limits"),
    (_hook("before_scan", {"a": float("nan")}), "not a finite number"),
    (_hook("after_scan", {"a": "lots"}), "is not a number"),
    (_hook("before_scan", action="vna.take_reference"),
     "unknown action 'vna.take_reference'"),
    ({"when": "befor_scan", "action": "call", "args": {}}, "unknown moment"),
    ({"when": "before_scan", "action": "cal"}, "hook action 'cal' is not known"),
    ({"when": "before_scan", "action": "call", "args": {"set": [1, 2]}},
     "'set' must map parameter ids"),
    # the ordered form is checked step by step, like the original one
    (_hook_steps("before_scan", [{"action": "X"}, {"action": "nope"}]),
     "unknown action 'nope'"),
    (_hook_steps("after_scan", [{"action": "X"}, {"set": {"a": 900.0}}]),
     "outside its limits"),
    (_hook_steps("before_scan", "X"), "'steps' must be a list"),
    (_hook_steps("before_scan", [{"run": "X"}]), "step 1 must be"),
    ({"when": "before_scan", "action": "call",
      "args": {"steps": [{"action": "X"}], "action": "X"}}, "not both"),
])
def test_validate_names_the_problem(hook, expected):
    errs = _errs([hook])
    assert any(expected in e for e in errs), errs


def test_the_call_hook_itself_refuses_before_moving_anything():
    """For a caller that did not validate: an unknown action must be found
    BEFORE the magnet is sent to the reference field."""
    from scan_core.hooks import run_hooks
    reg, events, _ = _diary_registry()
    with pytest.raises(KeyError, match="no action 'missing'"):
        run_hooks([_hook("before_scan", {"a": 3.0}, "missing")], "before_scan",
                  {"registry": reg, "current": {}})
    assert events == []


# ────────────────────────── the simulator, end to end ─────────────────────────

def test_the_sim_registry_offers_a_reference_and_u():
    reg = build_sim_registry()
    assert reg.get_action("vna_reference") is not None
    u = reg.get("u")
    assert u.dtype == "complex" and u.is_array
    assert u.acquire is reg.get("s21").acquire      # one sweep for both


def test_u_is_nan_without_a_reference():
    reg = build_sim_registry()
    reg._state.trigger_vna()
    assert np.all(np.isnan(reg.get("u").get()))


def test_sim_reference_flow_gives_u_near_zero_at_the_reference_field():
    """Reference taken at 60 mT, then a sweep through 60 mT: at that point S is
    the reference trace again (up to noise), so u ~ 0 everywhere -- and at a
    different field the resonance shows up as a real signal."""
    reg = build_sim_registry()
    recipe = Recipe(axes=[{"type": "array", "param": "field", "values": [20.0, 60.0]}],
                    detectors=["u", "s21"],
                    hooks=[_hook("before_scan", {"field": 60.0}, "vna_reference"),
                           _hook("after_scan", {"field": 0.0})])
    ds = run(recipe, reg, created_iso="t")
    u = as_complex(ds, "u")
    at_ref = np.abs(u.sel(field=60.0).values)
    elsewhere = np.abs(u.sel(field=20.0).values)
    assert at_ref.max() < 0.03                      # noise only (0.002 per quadrature)
    assert elsewhere.max() > 0.1                    # a real resonance
    assert reg.get("field").get() == 0.0            # after_scan


def test_the_example_recipe_runs():
    reg = build_sim_registry()
    recipe = Recipe.load(RECIPES / "fmr_reference_field_scan.yaml")
    assert recipe.validate(reg) == []
    ds = run(recipe, reg, created_iso="t")
    assert ds.sizes["field"] == 36
    assert np.isfinite(as_complex(ds, "u").values).all()
    assert reg.get("field").get() == 0.0


# ────────────────────────────────── the GUI ───────────────────────────────────

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


def _routine_recipe(**kw):
    base = dict(name="ref scan",
                axes=[{"type": "linear", "param": "field", "start": 70.0,
                       "stop": 0.0, "num": 3}],
                detectors=["u"],
                hooks=[{"when": "before_point", "action": "wait_ms", "args": {"ms": 0}},
                       _hook("before_scan", {"field": 190.0}, "vna_reference"),
                       _hook("after_scan", {"field": 0.0})])
    base.update(kw)
    return Recipe(**base)


def test_the_card_offers_the_registry_actions(builder):
    combo = builder.routines["before_scan"].add_combo
    assert combo.itemText(0).startswith("＋ run an action")
    assert [combo.itemData(i) for i in range(combo.count())] == [
        None, "vna_reference", "sim_autofocus"]
    assert combo.isEnabled()


def test_picking_an_action_appends_a_step_and_resets_the_list(builder):
    section = builder.routines["before_scan"]
    combo = section.add_combo
    for aid in ("sim_autofocus", "vna_reference"):
        combo.activated.emit(combo.findData(aid))      # what a user's pick sends
        assert combo.currentIndex() == 0               # ready for the next one
    assert section.action_ids() == ["sim_autofocus", "vna_reference"]
    # refilling the list (a registry refresh) must not add a step by itself
    section.set_actions(builder.registry.actions())
    assert section.action_ids() == ["sim_autofocus", "vna_reference"]


def test_the_card_builds_call_hooks(builder):
    builder.add_axis("field")
    row = builder.add_routine_set("before_scan", "field", 190.0)
    assert row is not None
    assert builder.set_routine_action("before_scan", "vna_reference")
    builder.add_routine_set("after_scan", "field", 0.0)
    assert builder.build_recipe().hooks == [
        _hook("before_scan", {"field": 190.0}, "vna_reference"),
        _hook("after_scan", {"field": 0.0})]
    assert "before scan: Magnetic field = 190 mT, then vna_reference" in builder.detail.text()


def test_only_settables_go_into_a_routine(builder):
    assert builder.add_routine_set("before_scan", "lockin_r", 1.0) is None
    assert builder.add_routine_set("before_scan", "nope", 1.0) is None
    assert not builder.set_routine_action("before_scan", "nope")


def test_the_card_round_trips_through_yaml_and_keeps_other_hooks(builder, tmp_path):
    original = _routine_recipe()
    assert builder.load_recipe(original) == []
    assert [r.param.id for r in builder.routines["before_scan"].rows] == ["field"]
    assert builder.routines["before_scan"].action_id() == "vna_reference"
    assert builder.build_recipe().hooks == original.hooks   # same order, nothing lost

    path = tmp_path / "scan.yaml"
    builder.build_recipe().save(path)
    from apps.scan_builder import ScanBuilder
    other = ScanBuilder(build_sim_registry())
    try:
        assert other.load_recipe(Recipe.load(path)) == []
        assert other.build_recipe().hooks == original.hooks
    finally:
        other.close()


def test_the_card_round_trips_through_a_measurement_file(builder, tmp_path):
    builder.load_recipe(_routine_recipe())
    builder.per_pt.setValue(0.0)
    builder.run_scan(block=True)
    assert any("before_scan: run vna_reference done" == m for m in builder.run_log)
    assert builder.registry.get("field").get() == 0.0
    path = tmp_path / "m.nc"
    builder.dataset.to_netcdf(path)
    recipe = builder.recipe_from_file(str(path))
    builder.load_recipe(Recipe(name="blank"))
    assert builder.build_recipe().hooks == []
    assert builder.load_recipe(recipe) == []
    assert builder.build_recipe().hooks == _routine_recipe().hooks


def test_two_old_routines_at_one_moment_load_as_one_ordered_list(builder):
    """An older definition said "set A, run X, then set B, run Y" as two call
    hooks at the same moment (the card then showed only the first). They load
    as ONE list of steps in the same order, and save back as one routine in
    the ordered form."""
    hooks = [_hook("before_scan", {"field": 190.0}, "vna_reference"),
             _hook("before_scan", {"rf_power": 3.0}, "sim_autofocus")]
    assert builder.load_recipe(_routine_recipe(hooks=hooks)) == []
    section = builder.routines["before_scan"]
    assert [s.to_step() for s in section.steps] == [
        {"set": {"field": 190.0}}, {"action": "vna_reference"},
        {"set": {"rf_power": 3.0}}, {"action": "sim_autofocus"}]
    assert builder.build_recipe().hooks == [_hook_steps("before_scan", [
        {"set": {"field": 190.0}}, {"action": "vna_reference"},
        {"set": {"rf_power": 3.0}}, {"action": "sim_autofocus"}])]


def test_a_foreign_hook_between_two_routines_keeps_them_apart(builder):
    """If another hook fires at the same moment BETWEEN two routines, one list
    cannot say "run that in the middle": the second routine is kept verbatim."""
    hooks = [_hook("before_scan", {"field": 190.0}, "vna_reference"),
             {"when": "before_scan", "action": "wait_ms", "args": {"ms": 0}},
             _hook("before_scan", {"rf_power": 3.0})]
    builder.load_recipe(_routine_recipe(hooks=hooks))
    assert len(builder.routines["before_scan"].steps) == 2
    assert builder.build_recipe().hooks == hooks
    builder.routines["before_scan"].rows[0].value_box.setValue(180.0)
    assert builder.build_recipe().hooks[0]["args"]["set"] == {"field": 180.0}
    assert builder.build_recipe().hooks[2] == hooks[2]


def test_missing_routine_parameters_and_actions_are_flagged(builder):
    hooks = [_hook("before_scan", {"mag2d.field": 150.0, "field": 5.0},
                   "vna.take_reference"),
             _hook("after_scan", {"mag2d.angle": 0.0})]
    missing = builder.load_recipe(_routine_recipe(hooks=hooks))
    assert {"mag2d.field", "vna.take_reference", "mag2d.angle"} <= set(missing)
    # what IS available still loads
    assert [r.param.id for r in builder.routines["before_scan"].rows] == ["field"]


def test_swapping_the_registry_drops_the_routines(builder):
    builder.load_recipe(_routine_recipe())
    assert builder.has_definition()
    builder.set_registry(build_sim_registry())
    assert builder.routines["before_scan"].rows == []
    assert builder.routines["before_scan"].action_id() is None
    assert builder.build_recipe().hooks == []


def test_a_routine_counts_as_a_definition_worth_keeping(builder):
    assert not builder.has_definition()
    builder.set_routine_action("after_scan", "vna_reference")
    assert builder.has_definition()


def test_an_invalid_routine_shows_in_the_summary(builder):
    builder.add_axis("field")
    row = builder.add_routine_set("before_scan", "rf_power")
    row.value_box.setRange(-1e6, 1e6)
    row.value_box.setValue(900.0)
    assert "invalid" in builder.summary.text()
    assert "before_scan routine: 'rf_power' = 900 is outside its limits" in builder.detail.text()


# ───────────── the card: an ordered list of steps (2026-09-25) ─────────────────

def test_the_card_holds_several_steps_in_order(builder):
    """Lukas: before the scan find focus, then save the pattern, then save a
    picture -- several actions at one moment, in an order he chooses."""
    builder.add_axis("field")
    assert builder.add_routine_action("before_scan", "sim_autofocus") is not None
    builder.add_routine_set("before_scan", "field", 190.0)
    builder.add_routine_action("before_scan", "vna_reference")
    builder.add_routine_set("before_scan", "field", 0.0)     # after an action: a NEW step
    assert builder.add_routine_action("before_scan", "nope") is None
    section = builder.routines["before_scan"]
    assert [s.marker.text() for s in section.steps] == ["1", "2", "3", "4"]
    hook = builder.build_recipe().hooks[0]
    assert hook == _hook_steps("before_scan", [
        {"action": "sim_autofocus"}, {"set": {"field": 190.0}},
        {"action": "vna_reference"}, {"set": {"field": 0.0}}])
    assert builder.build_recipe().validate(builder.registry) == []
    assert ("before scan: sim_autofocus, then Magnetic field = 190 mT, then "
            "vna_reference, then Magnetic field = 0 mT") in builder.detail.text()


def test_a_parameter_set_twice_before_any_action_is_one_step(builder):
    """Two setpoints with nothing run in between: only the last would count."""
    builder.add_routine_set("before_scan", "field", 190.0)
    builder.add_routine_set("before_scan", "rf_power", 3.0)
    builder.add_routine_set("before_scan", "field", 150.0)
    section = builder.routines["before_scan"]
    assert [s.to_step() for s in section.steps] == [
        {"set": {"field": 150.0}}, {"set": {"rf_power": 3.0}}]


def test_steps_move_up_and_down_and_the_hook_follows(builder):
    section = builder.routines["after_scan"]
    builder.add_routine_action("after_scan", "vna_reference")
    builder.add_routine_set("after_scan", "field", 0.0)
    assert "steps" in builder.build_recipe().hooks[0]["args"]
    assert not section.steps[0].up_btn.isEnabled()
    assert not section.steps[-1].down_btn.isEnabled()
    section.steps[1].up_btn.click()                   # field 0 first, then the action
    assert [s.marker.text() for s in section.steps] == ["1", "2"]
    # sets then ONE action: written in the original form, readable by older code
    assert builder.build_recipe().hooks == [_hook("after_scan", {"field": 0.0},
                                                  "vna_reference")]
    section.steps[0].down_btn.click()
    section.remove_step(section.steps[0])
    assert builder.build_recipe().hooks == [_hook("after_scan", {"field": 0.0})]


def test_set_routine_action_still_means_the_one_action(builder):
    builder.add_routine_action("before_scan", "sim_autofocus")
    builder.add_routine_action("before_scan", "vna_reference")
    assert builder.set_routine_action("before_scan", "sim_autofocus")
    assert builder.routines["before_scan"].action_ids() == ["sim_autofocus"]
    assert builder.set_routine_action("before_scan", None)
    assert builder.routines["before_scan"].steps == []


def test_ordered_steps_round_trip_through_yaml_and_nc(builder, tmp_path):
    steps_before = [{"action": "sim_autofocus"}, {"set": {"field": 190.0}},
                    {"action": "vna_reference"}, {"set": {"field": 60.0}}]
    steps_after = [{"action": "sim_autofocus"}, {"set": {"field": 0.0}},
                   {"action": "sim_autofocus"}]
    original = _routine_recipe(hooks=[
        {"when": "before_point", "action": "wait_ms", "args": {"ms": 0}},
        _hook_steps("before_scan", steps_before),
        _hook_steps("after_scan", steps_after),
        _hook_steps("each_sweep", [{"action": "sim_autofocus"}, {"set": {"rf_power": 2.0}},
                                   {"action": "sim_autofocus"}],
                    axis="field", edge="start", every=1, on_error="continue")])
    assert builder.load_recipe(original) == []
    assert builder.routines["before_scan"].action_ids() == ["sim_autofocus", "vna_reference"]
    assert len(builder.throughout) == 1 and len(builder.throughout[0].steps) == 3
    assert builder.build_recipe().hooks == original.hooks

    path = tmp_path / "scan.yaml"
    builder.build_recipe().save(path)
    from apps.scan_builder import ScanBuilder
    other = ScanBuilder(build_sim_registry())
    try:
        assert other.load_recipe(Recipe.load(path)) == []
        assert other.build_recipe().hooks == original.hooks
    finally:
        other.close()

    # and through a measurement file, having RUN in that order
    builder.per_pt.setValue(0.0)
    builder.run_scan(block=True)
    done = [m for m in builder.run_log if m.endswith(" done")]
    assert done[:4] == ["before_scan: run sim_autofocus done",
                        "before_scan: set field = 190 mT done",
                        "before_scan: run vna_reference done",
                        "before_scan: set field = 60 mT done"]
    assert done[-3:] == ["after_scan: run sim_autofocus done",
                         "after_scan: set field = 0 mT done",
                         "after_scan: run sim_autofocus done"]
    nc = tmp_path / "m.nc"
    builder.dataset.to_netcdf(nc)
    builder.load_recipe(Recipe(name="blank"))
    assert builder.load_recipe(builder.recipe_from_file(str(nc))) == []
    assert builder.build_recipe().hooks == original.hooks


def test_missing_ids_inside_steps_are_flagged_and_the_rest_loads(builder):
    hooks = [_hook_steps("before_scan", [
        {"action": "camera.autofocus"}, {"set": {"field": 5.0}},
        {"action": "camera.save_scan_pattern"}, {"action": "vna_reference"},
        {"set": {"mag2d.field": 0.0}}])]
    missing = builder.load_recipe(_routine_recipe(hooks=hooks))
    assert {"camera.autofocus", "camera.save_scan_pattern", "mag2d.field"} <= set(missing)
    assert [s.to_step() for s in builder.routines["before_scan"].steps] == [
        {"set": {"field": 5.0}}, {"action": "vna_reference"}]


def test_a_before_scan_hook_with_keys_the_card_cannot_show_is_kept(builder):
    """An on_error on a before-scan routine has no widget: saving it through
    the card would silently drop it, so the hook is kept verbatim."""
    hooks = [_hook("before_scan", {"field": 5.0}, "vna_reference", on_error="continue")]
    builder.load_recipe(_routine_recipe(hooks=hooks))
    assert builder.routines["before_scan"].steps == []
    assert builder.build_recipe().hooks == hooks
