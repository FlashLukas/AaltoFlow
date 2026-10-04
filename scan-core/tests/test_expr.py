"""expr.py: the restricted evaluator behind wait_until / abort_if / skip_if /
compute_set. What it must do (precedence, comparisons, the four functions) and,
above all, what it must REFUSE -- at parse time, before any value is read: the
text comes from a recipe file that anybody can edit and that is re-run from
every .nc.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import build_sim_registry                               # noqa: E402
from scan_core.expr import (ExprError, check, evaluate, fill_placeholders,  # noqa: E402
                            format_value, parse)


def ev(text, **values):
    """Evaluate with dotted names given as a__b=... (a.b)."""
    vals = {k.replace("__", "."): v for k, v in values.items()}
    return parse(text).evaluate(lambda pid: vals[pid])


# ───────────────────────────── allowed constructs ─────────────────────────────

@pytest.mark.parametrize("text, want", [
    ("1 + 2 * 3", 7.0),                    # * before +
    ("(1 + 2) * 3", 9.0),
    ("2 ** 3 ** 2", 512.0),                # ** is right-associative
    ("-2 ** 2", -4.0),                     # unary minus binds looser than **
    ("10 / 4", 2.5),                       # true division
    ("2.8e9 + 28e6 * 10", 3.08e9),
    ("+5", 5.0),
    ("abs(-3)", 3.0),
    ("min(4, 2, 7)", 2.0),
    ("max(4, 2, 7)", 7.0),
    ("round(2.567)", 3.0),
    ("round(2.567, 1)", 2.6),
    ("1 < 2", True),
    ("1 < 2 < 3", True),                   # chained
    ("1 < 3 < 2", False),
    ("3 >= 3 and 2 != 3", True),
    ("not 1 > 2", True),
    ("1 > 2 or 2 > 1", True),
    ("True and not False", True),
    ("'Stable' == 'Stable'", True),
    ("'a' != 'b'", True),
])
def test_allowed_constructs(text, want):
    assert ev(text) == pytest.approx(want) if isinstance(want, float) else ev(text) == want


def test_parameters_are_dotted_ids_read_once_each():
    reads = []

    def lookup(pid):
        reads.append(pid)
        return {"ppms.temperature": 10.02, "clMag.field": 5.0}[pid]

    e = parse("abs(ppms.temperature - 10) < 0.05 and ppms.temperature > 9")
    assert e.names == ["ppms.temperature"]
    assert e.evaluate(lookup) is True
    assert reads == ["ppms.temperature"]              # once, though used twice
    assert parse("2.8e9 + 28e6 * clMag.field").evaluate(lookup) == pytest.approx(2.94e9)


def test_plain_ids_and_text_values():
    assert ev("field * 2", field=3.0) == 6.0
    assert ev("ppms.state == 'Stable'", ppms__state="Stable") is True
    assert ev("camera.point_settled == False", camera__point_settled=False) is True


def test_short_circuit():
    # the right side of `and` is not evaluated once the left is false: a
    # division by zero there must not fire
    assert ev("x > 1 and 1 / x > 0", x=0.0) is False


# ─────────────────────────────── refused at PARSE ─────────────────────────────

@pytest.mark.parametrize("text, words", [
    ("__import__('os').system('dir')", "not allowed"),
    ("open('x', 'w')", "calling open()"),
    ("eval('1')", "calling eval()"),
    ("exec('1')", "calling exec()"),
    ("print(1)", "calling print()"),
    ("().__class__.__bases__[0].__subclasses__()", "not allowed"),
    ("field.__class__", "not a parameter id"),
    ("'abc'.upper()", "not allowed"),
    ("abs(field).real", "attributes are only allowed"),
    ("x[0]", "indexing"),
    ("lambda: 1", "lambda"),
    ("[i for i in range(3)]", "comprehensions"),
    ("{1, 2}", "sets"),
    ("[1, 2]", "lists"),
    ("(1, 2)", "tuples"),
    ("{'a': 1}", "dicts"),
    ("1 if x else 2", "if c else"),
    ("f'{x}'", "f-strings"),
    ("(y := 3)", ":="),
    ("7 % 3", "%"),
    ("7 // 3", "//"),
    ("1 << 3", "<<"),
    ("~1", "~"),
    ("x in [1]", "comparison"),
    ("x is None", "comparison"),
    ("None", "constant"),
    ("1j", "constant"),
    ("b'x'", "constant"),
    ("abs(x=1)", "keyword"),
    ("abs(1, 2)", "1 argument"),
    ("min(1)", "2 or more"),
    ("round(1, 2, 3)", "1 to 2"),
    ("max(*x, 1)", "unpacking"),
    ("x = 1", "not a valid expression"),
    ("import os", "not a valid expression"),
    ("1; 2", "not a valid expression"),
    ("", "empty"),
    ("   ", "empty"),
    ("1 +", "not a valid expression"),
    ("9" * 900, "too large"),              # a huge integer literal
    ("(" * 300 + "1" + ")" * 300, ""),     # deep nesting: an error, not a crash
    ("1 + " * 600 + "1", "longer than"),
])
def test_forbidden_constructs_fail_at_parse(text, words):
    with pytest.raises(ExprError) as info:
        parse(text)
    assert words in str(info.value)


def test_nothing_is_read_or_run_for_a_refused_expression():
    reads = []
    with pytest.raises(ExprError):
        parse("field + open('x')").evaluate(lambda pid: reads.append(pid) or 1.0)
    assert reads == []


def test_not_text_is_refused():
    with pytest.raises(ExprError):
        parse(42)


# ─────────────────────────── refused at EVALUATION ────────────────────────────

@pytest.mark.parametrize("text, values, words", [
    ("1 / x", {"x": 0.0}, "division by zero"),
    ("10 ** 400", {}, "overflow"),
    ("2 ** x", {"x": 1e9}, "overflow"),
    ("'a' * 3", {}, "needs numbers"),       # no string repetition (or + of text)
    ("s + 1", {"s": "ok"}, "needs numbers"),
    ("s < 3", {"s": "ok"}, "cannot compare"),
    ("abs(s)", {"s": "ok"}, "needs numbers"),
    ("round(x, 1.5)", {"x": 1.0}, "whole number"),
    ("(-8) ** 0.5", {}, ""),                # complex result -> an error
])
def test_runtime_errors_are_expr_errors(text, values, words):
    with pytest.raises(ExprError) as info:
        parse(text).evaluate(lambda pid: values[pid])
    assert words in str(info.value)


def test_a_value_that_is_not_one_number_is_refused():
    import numpy as np
    with pytest.raises(ExprError, match="not a single value"):
        parse("x > 1").evaluate(lambda pid: np.arange(3.0))
    assert parse("x > 1").evaluate(lambda pid: np.float64(2.0)) is True
    assert parse("x").evaluate(lambda pid: np.bool_(True)) is True
    with pytest.raises(ExprError, match="complex"):
        parse("x > 1").evaluate(lambda pid: 1 + 2j)
    with pytest.raises(ExprError, match="no value"):
        parse("x > 1").evaluate(lambda pid: None)


def test_nan_makes_comparisons_false():
    assert ev("x > 1", x=float("nan")) is False
    assert ev("x < 1", x=float("nan")) is False


# ──────────────────────────────── the registry ────────────────────────────────

def test_check_against_the_registry():
    reg = build_sim_registry()
    assert check("field > 10 and rf_freq < 3000", reg) == []
    assert check("lockin_r > 0.1", reg) == []          # a detector: its current value
    assert check("lockin_state == 'ok'", reg) == []    # an enum reads as text
    errs = check("fieldd > 10", reg)
    assert errs and "unknown parameter 'fieldd'" in errs[0]
    assert "whole trace" in check("s21 > 1", reg)[0]   # an array detector
    assert check("1 +", reg) and "not a valid" in check("1 +", reg)[0]
    assert check("open('x')", reg)                     # forbidden before unknown


def test_evaluate_reads_the_registry():
    reg = build_sim_registry()
    reg.get("field").set(42.0)
    assert evaluate("field * 2", reg) == 84.0
    assert evaluate("field > 40 and field < 50", reg) is True


def test_placeholders():
    reg = build_sim_registry()
    reg.get("field").set(12.3456789)
    text, bad = fill_placeholders("B = {field} mT; T = {ppms.temperature}; {not an id}", reg)
    assert text == "B = 12.3457 mT; T = {ppms.temperature}; {not an id}"
    assert bad == ["ppms.temperature"]
    assert format_value(True) == "True" and format_value("ok") == "ok"
    assert format_value(1e-7) == "1e-07"
