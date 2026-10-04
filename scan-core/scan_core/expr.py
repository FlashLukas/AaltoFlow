"""
expr.py -- the small, SAFE expression language of the routine steps.

Three routine steps take a condition or a formula written as text:

    wait_until  {condition: "ppms.temperature < 10.05 and ppms.temperature > 9.95", ...}
    abort_if    {condition: "hf2.r1 > 0.9"}
    compute_set {set: {smb.frequency: "2.8e9 + 28e6 * clMag.field"}}

and the text comes from a recipe file -- which anybody can edit, and which is
stored in every .nc and re-run from there. Python's eval() would run whatever
that text says (delete files, open sockets), so it is never used here, nor
exec(). Instead the text is PARSED with Python's own parser (`ast`), and the
tree is walked by a few lines of code that only know how to do the handful of
things listed below. Anything else is refused when the recipe is VALIDATED,
i.e. before the scan moves anything -- not in the middle of a night's run.

WHAT IS ALLOWED
  numbers (1, 2.5, 3e9), strings ('Stable'), True / False
  parameter ids, dotted as they are in the registry: ppms.temperature,
      clMag.field, field (the simulator has no prefixes)
  + - * / **   and unary -  (on numbers only: no "a" * 10)
  < <= > >= == !=   (chained too: 9.95 < ppms.temperature < 10.05)
  and  or  not   ( )
  abs(x)  min(a, b, ...)  max(a, b, ...)  round(x)  round(x, n)

WHAT IS REFUSED (each with a message naming it)
  any other function call, attributes that are not part of a parameter id
  ("x".upper, field.real), indexing [..], lambda, comprehensions, if/else
  expressions, lists/tuples/dicts, f-strings, :=, % and //, bit operations.

WHAT A PARAMETER ID READS -- and what it does NOT do
  A settable reads its READBACK, a detector its CURRENT value: on the lab both
  come from the module's status stream (the cached status frame, refreshed
  5-10 times a second), on the simulator from its state. Evaluating an
  expression NEVER triggers a new acquisition: a lock-in's `acquire`d sample
  reads as the last one taken, a VNA trace not at all (array detectors are
  refused -- a condition needs one number). If you need a fresh measurement in
  a condition, put the point's own detectors in the scan, and use the value
  they latch.

Numbers are computed as floats, so 10**400 is an "overflow" error, not a
number with 400 digits that takes the machine with it. `==` on floats is exact:
write abs(a - b) < 0.01 for "equal enough".
"""

from __future__ import annotations

import ast
import math
import operator
import re

#: Longest expression accepted. A condition is a line, not a program; the
#: limit also keeps the parser's recursion far from Python's own limit.
MAX_LENGTH = 1000

#: The functions an expression may call: name -> (min args, max args or None).
FUNCTIONS = {"abs": (1, 1), "min": (2, None), "max": (2, None), "round": (1, 2)}

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow}
_BINOP_TEXT = {ast.Mod: "%", ast.FloorDiv: "//", ast.MatMult: "@",
               ast.LShift: "<<", ast.RShift: ">>", ast.BitOr: "|",
               ast.BitXor: "^", ast.BitAnd: "&"}
_CMPOPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}

#: Friendly names for the node types people are most likely to try.
_REFUSED = {
    "Subscript": "indexing with [ ]", "Lambda": "lambda",
    "ListComp": "comprehensions", "SetComp": "comprehensions",
    "DictComp": "comprehensions", "GeneratorExp": "comprehensions",
    "IfExp": "'x if c else y' expressions", "List": "lists", "Tuple": "tuples",
    "Dict": "dicts", "Set": "sets", "JoinedStr": "f-strings",
    "FormattedValue": "f-strings", "NamedExpr": "':=' assignments",
    "Starred": "'*' unpacking", "Await": "await", "Yield": "yield",
    "YieldFrom": "yield", "Slice": "slices",
}


class ExprError(ValueError):
    """An expression that is not allowed, does not parse, or cannot be computed."""


class Expression:
    """A parsed, CHECKED expression. Build it with parse(); run it with evaluate().

    `names` = the parameter ids it reads, in the order they first appear.
    """

    def __init__(self, text: str, tree: ast.AST, names: list[str]):
        self.text = text
        self._tree = tree
        self.names = names

    def evaluate(self, lookup):
        """Compute the value. `lookup(pid)` returns a parameter's value; each
        id is looked up ONCE per evaluation, however often it appears."""
        values = {}
        for pid in self.names:
            values[pid] = _as_value(lookup(pid), pid)
        try:
            return _eval(self._tree, values)
        except ExprError:
            raise
        except OverflowError:
            raise ExprError(f"{self.text!r}: a number got too large (overflow)") from None
        except ZeroDivisionError:
            raise ExprError(f"{self.text!r}: division by zero") from None
        except (TypeError, ValueError) as exc:
            raise ExprError(f"{self.text!r}: {exc}") from None

    def __repr__(self):
        return f"Expression({self.text!r})"


def parse(text) -> Expression:
    """Parse and check `text`. Raises ExprError naming what is not allowed."""
    if not isinstance(text, str):
        raise ExprError(f"an expression must be text, not {type(text).__name__}")
    src = text.strip()
    if not src:
        raise ExprError("the expression is empty")
    if len(src) > MAX_LENGTH:
        raise ExprError(f"the expression is longer than {MAX_LENGTH} characters")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        where = f" (at character {exc.offset})" if exc.offset else ""
        raise ExprError(f"{src!r} is not a valid expression: {exc.msg}{where}") from None
    except (ValueError, RecursionError, MemoryError) as exc:
        # a 5000-digit integer literal, or brackets nested a thousand deep
        raise ExprError(f"{src!r} cannot be read ({type(exc).__name__})") from None
    names: list[str] = []
    try:
        _check(tree.body, names)
    except RecursionError:
        raise ExprError(f"{src!r} is nested too deeply") from None
    return Expression(src, tree.body, names)


def _dotted(node) -> str | None:
    """'ppms.temperature' for Attribute(Name('ppms'), 'temperature'); None if
    the chain does not end in a plain name (e.g. "abc".upper)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _check(node, names: list[str]) -> None:
    """Walk the tree; refuse every node type that is not on the list."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or isinstance(v, str):
            return
        if isinstance(v, (int, float)):
            try:
                float(v)                       # a huge int literal: refuse now
            except OverflowError:
                raise ExprError("a number in the expression is too large") from None
            return
        raise ExprError(f"the constant {v!r} is not allowed (numbers, text, "
                        f"True and False only)")
    if isinstance(node, (ast.Name, ast.Attribute)):
        pid = _dotted(node)
        if pid is None:
            raise ExprError("attributes are only allowed as part of a parameter "
                            "id (like ppms.temperature)")
        if any(part.startswith("__") for part in pid.split(".")):
            raise ExprError(f"{pid!r} is not a parameter id")
        if pid not in names:
            names.append(pid)
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            op = _BINOP_TEXT.get(type(node.op), type(node.op).__name__)
            raise ExprError(f"the operator {op} is not allowed "
                            f"(use + - * / ** only)")
        _check(node.left, names)
        _check(node.right, names)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            raise ExprError("the operator ~ is not allowed")
        _check(node.operand, names)
        return
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _check(v, names)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMPOPS:
                raise ExprError(f"the comparison {type(op).__name__} is not "
                                f"allowed (use < <= > >= == != only)")
        _check(node.left, names)
        for c in node.comparators:
            _check(c, names)
        return
    if isinstance(node, ast.Call):
        fn = node.func.id if isinstance(node.func, ast.Name) else None
        if fn not in FUNCTIONS:
            shown = fn or (_dotted(node.func) or "this")
            raise ExprError(f"calling {shown}() is not allowed (only "
                            f"{', '.join(FUNCTIONS)})")
        if node.keywords:
            raise ExprError(f"{fn}() takes no keyword arguments here")
        lo, hi = FUNCTIONS[fn]
        n = len(node.args)
        if n < lo or (hi is not None and n > hi):
            want = f"{lo}" if lo == hi else (f"{lo} or more" if hi is None
                                              else f"{lo} to {hi}")
            raise ExprError(f"{fn}() takes {want} argument(s), not {n}")
        for a in node.args:
            _check(a, names)
        return
    what = _REFUSED.get(type(node).__name__, type(node).__name__)
    raise ExprError(f"{what} not allowed in an expression")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, str)


def _as_value(v, pid: str):
    """A parameter's value as the evaluator wants it: float, bool or str."""
    if isinstance(v, (bool, str)):
        return v
    try:
        import numpy as np
        if isinstance(v, np.bool_):
            return bool(v)
        if isinstance(v, np.ndarray):
            if v.ndim == 0:
                v = v.item()
            else:
                raise ExprError(f"{pid} is not a single value (an array of {v.size})")
    except ImportError:                                    # pragma: no cover
        pass
    if isinstance(v, (bool, str)):
        return v
    if v is None:
        raise ExprError(f"{pid} has no value yet")
    if isinstance(v, complex):
        raise ExprError(f"{pid} is complex; use a real detector in an expression")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ExprError(f"{pid} = {v!r} is not a number or text") from None


def _num(v, what: str):
    if isinstance(v, str) or not _is_number(v):
        raise ExprError(f"{what} needs numbers, got {v!r}")
    return float(v)


def _eval(node, values):
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, (bool, str)):
            return v
        return float(v)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return values[_dotted(node)]
    if isinstance(node, ast.BinOp):
        sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
               ast.Pow: "**"}[type(node.op)]
        a = _num(_eval(node.left, values), f"'{sym}'")
        b = _num(_eval(node.right, values), f"'{sym}'")
        if isinstance(node.op, ast.Pow):
            r = math.pow(a, b)                 # raises on overflow, no bigints
        else:
            r = _BINOPS[type(node.op)](a, b)
        if isinstance(r, complex):             # pragma: no cover (math.pow refuses)
            raise ExprError("the result is complex")
        return r
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, values)
        if isinstance(node.op, ast.Not):
            return not v
        v = _num(v, "unary -" if isinstance(node.op, ast.USub) else "unary +")
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BoolOp):
        # Python's own short-circuit semantics: `a and b` stops at a false a
        if isinstance(node.op, ast.And):
            v = True
            for sub in node.values:
                v = _eval(sub, values)
                if not v:
                    return v
            return v
        v = False
        for sub in node.values:
            v = _eval(sub, values)
            if v:
                return v
        return v
    if isinstance(node, ast.Compare):
        left = _eval(node.left, values)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, values)
            if type(op) not in (ast.Eq, ast.NotEq) and (
                    isinstance(left, str) != isinstance(right, str)):
                raise ExprError(f"cannot compare {left!r} and {right!r} with "
                                f"an ordering (<, >)")
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        fn = node.func.id
        args = [_eval(a, values) for a in node.args]
        if fn == "abs":
            return abs(_num(args[0], "abs()"))
        if fn in ("min", "max"):
            nums = [_num(a, f"{fn}()") for a in args]
            return min(nums) if fn == "min" else max(nums)
        if fn == "round":
            x = _num(args[0], "round()")
            if len(args) == 1:
                return float(round(x))
            n = _num(args[1], "round()")
            if n != int(n):
                raise ExprError("round(x, n): n must be a whole number")
            return float(round(x, int(n)))
    raise ExprError(f"cannot evaluate {type(node).__name__}")   # pragma: no cover


# ─────────────────────────── against a registry ──────────────────────────────

def param_problem(registry, pid: str) -> str | None:
    """Why `pid` cannot be read in an expression, or None if it can."""
    p = registry.get(pid) if registry is not None else None
    if p is None:
        return f"unknown parameter '{pid}'"
    if getattr(p, "kind", "") not in ("settable", "gettable"):
        return f"'{pid}' cannot be read"
    if getattr(p, "axes", None):
        return f"'{pid}' is a whole trace, not one value"
    if getattr(p, "dtype", "float") == "complex":
        return f"'{pid}' is complex, not one real value"
    return None


def check(text, registry=None) -> list[str]:
    """Every problem with `text` as an expression over `registry` ([] = fine).

    For recipe.validate() and for the Scan Builder's live red border: the same
    function, so a formula the builder accepts is one the run accepts.
    """
    try:
        expr = parse(text)
    except ExprError as exc:
        return [str(exc)]
    if registry is None:
        return []
    return [msg for pid in expr.names
            if (msg := param_problem(registry, pid)) is not None]


def read_value(registry, pid: str):
    """The CURRENT value of a parameter (the readback / the cached status
    value -- never a new acquisition; see the module docstring)."""
    p = registry.get(pid)
    if p is None:
        raise ExprError(f"unknown parameter '{pid}'")
    return p.get()


def evaluate(expr, registry):
    """Parse (if text) and compute `expr`, reading parameters from `registry`."""
    if not isinstance(expr, Expression):
        expr = parse(expr)
    return expr.evaluate(lambda pid: read_value(registry, pid))


def format_value(v) -> str:
    """How a value is written into a comment or a log line: %.6g for numbers."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, str):
        return v
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return str(v)


#: {param.id} in a comment's text
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")


def fill_placeholders(text: str, registry) -> tuple[str, list[str]]:
    """Replace each {param.id} in `text` by that parameter's current value.

    Returns (filled text, [ids that could not be filled]). An unknown id, or a
    read that fails, is LEFT AS WRITTEN -- a comment with "{ppms.tmeperature}"
    in it is still a comment worth keeping, and the caller warns about it.
    """
    bad: list[str] = []

    def one(m):
        pid = m.group(1)
        if param_problem(registry, pid) is not None:
            bad.append(pid)
            return m.group(0)
        try:
            return format_value(_as_value(read_value(registry, pid), pid))
        except Exception:
            bad.append(pid)
            return m.group(0)

    return _PLACEHOLDER.sub(one, text), bad
