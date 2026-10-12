"""predictors.py -- WHERE SHOULD THE PEAK BE? The window's physics, pluggable.

The window (window.py) sweeps a slow array detector only near the peak (or
dip) it expects at each point. It used to know one piece of physics: FMR,
Kittel's formula from the field and its angle. Lukas, 2026-10-12: "add option
for another physics". So the window now knows only this:

    a PREDICTOR maps the current INPUTS (numbers read at the point: a field,
    a temperature, any knob) and its PARAMETERS (numbers in the recipe: a
    g-factor, an offset) to the expected peak position -- or NaN, "the model
    does not apply here, sweep the full band". It may name ONE parameter it
    can TRACK: correct from every clean measured peak.

and every piece of physics is a predictor in a small REGISTRY here:

    kittel      FMR (the old window, numbers unchanged: resonance.py)
    linear      peak = a + b * x of one input; tracks the offset a
    expression  a formula you type, e.g. "0.028*sqrt(B*(B+meff))", over
                named inputs and parameters; tracks one parameter by a
                1-D root find. Parsed by a small whitelist (numbers, names,
                + - * / **, sqrt abs sin cos exp log, pi) -- never eval().

A LAB'S OWN predictor is a Python file in `scan-core/lab_predictors/` that
builds a Predictor subclass and calls `register(...)` (README, "Window
predictors"). Those files are imported the first time the registry is asked.

Units. A predictor declares the unit its answer is in (`output_unit`, e.g.
"Hz"; "" = the detector's own axis unit); the window converts between that
and the detector's axis unit when both are in one family (units.py: Hz/MHz,
nm/um, ...). An input may declare a unit FAMILY ("field", "angle") and the
unit the formula wants it in: a field read in T is then handed over in mT.
An input without a family is passed in whatever unit its knob has.

Nothing here talks to hardware, Qt or the engine.
"""

from __future__ import annotations

import ast
import importlib.util
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import resonance, units

# ─────────────────────────────── declarations ────────────────────────────────

#: unit families an INPUT may declare -> {unit as written (lower case): factor
#: to the family's working unit}. Field in mT and angle in degrees, as the
#: Kittel formulas are written. Oe and kA/m are here (not in units.py) because
#: magnet modules do declare them and Kittel needs mu0 H.
INPUT_FAMILIES = {
    "field": {"": 1.0, "mt": 1.0, "t": 1e3, "g": 0.1, "oe": 0.1,
              "ka/m": 4 * math.pi / 10},
    "angle": {"": 1.0, "deg": 1.0, "°": 1.0, "rad": 180.0 / math.pi},
}


@dataclass
class Input:
    """A number the predictor needs AT EACH POINT."""
    role: str                    # the key in the recipe's `inputs`
    label: str
    family: str | None = None    # "field" / "angle" (converted) or None (as read)
    unit: str = ""               # what the formula wants (display; "" = as read)
    required: bool = True        # False: `default` when the recipe gives none
    default: float = 0.0
    help: str = ""


@dataclass
class Param:
    """A number the predictor is GIVEN (the recipe's `params`)."""
    name: str
    label: str
    unit: str = ""               # display; "{axis}" = the detector's axis unit,
                                 # "{x}" = the unit of input "x"
    default: float | None = 0.0  # None = the recipe must give it
    lo: float = -1e12            # plausible range (validation + the GUI's spin box)
    hi: float = 1e12
    decimals: int = 4
    #: an estimate further than max(6 MAD, floor) from the running median is
    #: an outlier and ignored (only matters for the tracked parameter)
    floor: float | None = None
    help: str = ""


@dataclass
class Option:
    """A non-numeric setting (the recipe's `options`)."""
    name: str
    label: str
    choices: tuple = ()          # () = free text
    default: str = ""
    help: str = ""


class Predictor:
    """One piece of physics for the window. Subclass and register().

    Override `predict` (and `track` when a closed form exists; the default is
    a 1-D root find on the tracked parameter, which works for anything
    smooth). `inputs_for` / `params_for` may depend on the options (the
    expression predictor's names come from its formula)."""

    name = ""
    label = ""
    help = ""
    #: the unit predict() answers in ("" = the detector's axis unit)
    output_unit = ""
    inputs: tuple = ()
    params: tuple = ()
    options: tuple = ()
    #: the parameter `track: true` corrects (None = nothing to track)
    tracked = None

    # ---- the declaration (may depend on the recipe's window block) ----------
    def inputs_for(self, w: dict) -> list[Input]:
        """The inputs, for the window block `w` (its options, inputs, ...)."""
        return list(self.inputs)

    def params_for(self, w: dict) -> list[Param]:
        return list(self.params)

    def tracked_for(self, options: dict) -> str | None:
        return self.tracked

    def output_unit_for(self, options: dict) -> str:
        return self.output_unit

    def option_defaults(self) -> dict:
        return {o.name: o.default for o in self.options}

    def param_defaults(self, w: dict) -> dict:
        return {p.name: float(p.default) for p in self.params_for(w)
                if p.default is not None}

    # ---- the physics -------------------------------------------------------
    def predict(self, x: dict, p: dict, options: dict) -> float:
        """Expected peak (in output_unit) from inputs `x` and params `p`;
        NaN when the model does not apply here."""
        raise NotImplementedError

    def track(self, peak: float, x: dict, p: dict, options: dict) -> float:
        """The value of the tracked parameter that puts the peak at `peak`
        (output_unit) with everything else fixed; NaN = no answer."""
        name = self.tracked_for(options)
        if not name:
            return math.nan
        return solve_for(lambda t: self.predict(x, dict(p, **{name: t}), options),
                         peak, float(p[name]))

    def validate(self, options: dict) -> list[str]:
        """Problems with the options (the window checks inputs and params)."""
        errs = []
        for o in self.options:
            v = options.get(o.name, o.default)
            if o.choices and v not in o.choices:
                errs.append(f"window: {o.name} must be one of {', '.join(o.choices)}")
        return errs


# ─────────────────────────────── the registry ────────────────────────────────

_REGISTRY: dict[str, Predictor] = {}
#: problems importing lab_predictors/*.py (shown by validate, never fatal)
plugin_errors: list[str] = []
_plugins_loaded = False
#: where a lab keeps its own predictors (one .py each); not in the package,
#: so an update of scan-core never overwrites them
LAB_DIR = Path(__file__).resolve().parent.parent / "lab_predictors"


def register(pred: Predictor) -> Predictor:
    """Make `pred` available to recipes under `pred.name` (a later register
    with the same name replaces it: a lab may override a built-in)."""
    if not pred.name or not isinstance(pred.name, str):
        raise ValueError("a predictor needs a name")
    _REGISTRY[pred.name] = pred
    return pred


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    if not LAB_DIR.is_dir():
        return
    for path in sorted(LAB_DIR.glob("*.py")):
        name = f"scan_core_lab_predictor_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:            # a broken lab file must not kill scans
            plugin_errors.append(f"lab predictor {path.name}: {type(exc).__name__}: {exc}")


def get(name: str) -> Predictor | None:
    _load_plugins()
    return _REGISTRY.get(name)


def names() -> list[str]:
    _load_plugins()
    return list(_REGISTRY)


def all_predictors() -> list[Predictor]:
    _load_plugins()
    return list(_REGISTRY.values())


# ─────────────────────────────── helpers ─────────────────────────────────────

def solve_for(fn, target: float, start: float, max_expand: int = 60,
              iters: int = 200) -> float:
    """The t nearest `start` with fn(t) == target, or NaN.

    Brackets a sign change by stepping out from `start` in both directions
    (doubling the step), then bisects. Bisection cannot wander off the way
    Newton can, and a NaN of the model (not valid there) is simply stepped
    over. The bracket NEAREST the start wins, so of two roots (a parabola)
    the one the scan is already near is taken."""
    if not (math.isfinite(target) and math.isfinite(start)):
        return math.nan

    def g(t):
        try:
            v = fn(t) - target
        except (ArithmeticError, ValueError):
            return math.nan
        return v if math.isfinite(v) else math.nan

    g0 = g(start)
    if g0 == 0:
        return start
    h = max(abs(start) * 0.01, 1e-9)
    bracket = None
    prev = {+1: (start, g0), -1: (start, g0)}
    for _ in range(max_expand):
        for s in (+1, -1):
            t = start + s * h
            gt = g(t)
            tp, gp = prev[s]
            if math.isfinite(gt) and math.isfinite(gp) and (gt == 0 or gp * gt < 0):
                bracket = (tp, t, gp, gt)
                break
            if math.isfinite(gt):
                prev[s] = (t, gt)
        if bracket:
            break
        h *= 2
    if bracket is None:
        return math.nan
    a, b, ga, gb = bracket
    if gb == 0:
        return b
    for _ in range(iters):
        m = 0.5 * (a + b)
        gm = g(m)
        if not math.isfinite(gm):
            return math.nan
        if gm == 0 or abs(b - a) <= 1e-12 * max(1.0, abs(m)):
            return m
        if ga * gm < 0:
            b, gb = m, gm
        else:
            a, ga = m, gm
    return 0.5 * (a + b)


def output_factor(out_unit: str, axis_unit: str) -> float | None:
    """Axis units per one output unit (1e3 for GHz -> MHz), None if they do
    not convert. "" (= "already in the axis unit") is 1."""
    out, ax = (out_unit or "").strip(), (axis_unit or "").strip()
    if not out or out == ax:
        return 1.0
    a, b = units.family_of(out), units.family_of(ax)
    if a is None or b is None or a[0] != b[0]:
        return None
    return a[1] / b[1]


def unit_text(unit: str, axis_unit: str = "", input_units: dict | None = None) -> str:
    """A Param.unit for display, with {axis} and {<role>} filled in."""
    u = unit or ""
    u = u.replace("{axis}", axis_unit or "axis unit")
    for role, iu in (input_units or {}).items():
        u = u.replace("{" + role + "}", iu or "unit")
    return u


# ─────────────────────────────── kittel (FMR) ────────────────────────────────

class Kittel(Predictor):
    """FMR: the line from the field (and its in-plane angle) by Kittel's
    formula -- the window's original physics, unchanged (resonance.py)."""

    name = "kittel"
    label = "Kittel (FMR)"
    help = ("Ferromagnetic resonance. in-plane: field in the film plane, uniaxial\n"
            "in-plane anisotropy Hk (equilibrium angle solved); out-of-plane:\n"
            "field along the normal, f = gamma'(B - mu0 Meff), no line below Meff.")
    output_unit = "Hz"
    inputs = (
        Input("field", "field", family="field", unit="mT",
              help="The parameter whose value at each point IS the field."),
        Input("angle", "angle", family="angle", unit="deg", required=False,
              default=0.0, help="In-plane field angle (in-plane model only)."),
    )
    params = (
        Param("g", "g", "", 2.0, 0.5, 10.0, 4, help="g-factor"),
        Param("meff_mT", "μ0Meff", "mT", 1750.0, -5000.0, 5000.0, 1, floor=5.0,
              help="mu0 Meff ASSUMED at the start; tracked from every clean line."),
        Param("hk_mT", "Hk", "mT", 0.0, -1000.0, 1000.0, 2,
              help="in-plane uniaxial anisotropy field (in-plane model)"),
        Param("easy_axis_deg", "easy axis", "deg", 0.0, -360.0, 360.0, 1,
              help="direction of the easy axis (in-plane model)"),
    )
    options = (Option("model", "model", resonance.MODELS, "inplane",
                      help="in-plane or out-of-plane geometry"),)
    tracked = "meff_mT"

    def predict(self, x, p, options):
        try:
            return resonance.kittel_hz(options.get("model", "inplane"),
                                       x["field"], x.get("angle", 0.0), p)
        except (ArithmeticError, ValueError):
            return math.nan

    def track(self, peak, x, p, options):
        # closed form (resonance.meff_from_f): no root finding can wander off
        return resonance.meff_from_f(options.get("model", "inplane"), peak,
                                     x["field"], x.get("angle", 0.0), p)


# ─────────────────────────────── linear ──────────────────────────────────────

class Linear(Predictor):
    """peak = a + b * x: any knob that moves the peak about linearly (a
    temperature tuning a resonator, a gate voltage, a field over a short
    range). Tracks the offset a: the slope is usually known, the offset
    drifts."""

    name = "linear"
    label = "Linear  (peak = a + b·x)"
    help = ("peak = a + b * x of one input, in the detector's axis unit (or the\n"
            "unit set below). 'track' corrects a from every clean peak.")
    inputs = (Input("x", "x", help="The knob that moves the peak."),)
    params = (
        Param("a", "a", "{axis}", 0.0, -1e15, 1e15, 4, help="offset: the peak at x = 0"),
        Param("b", "b", "{axis}/{x}", 1.0, -1e15, 1e15, 6, help="slope per unit of x"),
    )
    options = (Option("unit", "result unit", (), "",
                      help="Unit of a and b (e.g. GHz); empty = the detector's axis unit."),)
    tracked = "a"

    def output_unit_for(self, options):
        return (options.get("unit") or "").strip()

    def predict(self, x, p, options):
        v = float(p["a"]) + float(p["b"]) * float(x["x"])
        return v if math.isfinite(v) else math.nan

    def track(self, peak, x, p, options):
        v = float(peak) - float(p["b"]) * float(x["x"])
        return v if math.isfinite(v) else math.nan


# ─────────────────────────────── expression ──────────────────────────────────

#: what a formula may call; angles in RADIANS (write sin(phi*pi/180) for deg)
FUNCTIONS = {"sqrt": math.sqrt, "abs": abs, "sin": math.sin, "cos": math.cos,
             "exp": math.exp, "log": math.log}
CONSTANTS = {"pi": math.pi}
MAX_FORMULA = 500

_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.Pow: lambda a, b: a ** b}
_UNARY = {ast.USub: lambda a: -a, ast.UAdd: lambda a: a}


class FormulaError(ValueError):
    """A formula that is not allowed or does not parse."""


class Formula:
    """A SAFE arithmetic formula: parsed once into Python's syntax tree, every
    node checked against a whitelist, evaluated by walking that tree. Nothing
    is ever handed to eval(): "__import__('os')" is a call of a name that is
    not in FUNCTIONS, an attribute or a string, all of which are refused.
    Every number is a float, so 9**9**9 overflows (NaN) instead of building
    an integer with billions of digits."""

    def __init__(self, text: str):
        if not isinstance(text, str) or not text.strip():
            raise FormulaError("the formula is empty")
        if len(text) > MAX_FORMULA:
            raise FormulaError(f"the formula is longer than {MAX_FORMULA} characters")
        self.text = text.strip()
        try:
            tree = ast.parse(self.text, mode="eval")
        except SyntaxError as exc:
            raise FormulaError(f"not a formula: {exc.msg}") from None
        self.names: list[str] = []
        self._check(tree.body)
        self._tree = tree.body

    def _check(self, node) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError(f"only numbers are allowed, not {node.value!r}")
        elif isinstance(node, ast.Name):
            if node.id in FUNCTIONS:
                raise FormulaError(f"{node.id} is a function: write {node.id}(...)")
            if node.id not in CONSTANTS and node.id not in self.names:
                if node.id.startswith("_"):
                    raise FormulaError(f"names may not start with '_' ({node.id})")
                self.names.append(node.id)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BINOPS:
                raise FormulaError("only + - * / ** are allowed")
            self._check(node.left)
            self._check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY:
                raise FormulaError("only + - * / ** are allowed")
            self._check(node.operand)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise FormulaError("only these functions are allowed: "
                                   + ", ".join(FUNCTIONS))
            if node.keywords or len(node.args) != 1:
                raise FormulaError(f"{node.func.id}() takes exactly one argument")
            self._check(node.args[0])
        else:
            raise FormulaError(f"{type(node).__name__} is not allowed in a formula "
                               f"(numbers, names, + - * / **, "
                               f"{', '.join(FUNCTIONS)}, pi)")

    def __call__(self, values: dict) -> float:
        """The value with `values` for the names; NaN on any math error."""
        try:
            v = float(self._eval(self._tree, values))
        except (ArithmeticError, ValueError, TypeError, KeyError):
            return math.nan
        return v if math.isfinite(v) else math.nan

    def _eval(self, node, values):
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            return float(values[node.id])
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, values), self._eval(node.right, values)
            r = _BINOPS[type(node.op)](a, b)
            if isinstance(r, complex):          # (-8) ** (1/3)
                raise ValueError("complex result")
            return r
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, values))
        # ast.Call, checked in _check
        return FUNCTIONS[node.func.id](self._eval(node.args[0], values))


class Expression(Predictor):
    """A formula typed in the recipe. Every free name in it is either an
    INPUT (the recipe's `inputs` maps it to a parameter id or a number) or a
    PARAMETER (in `params`, a number; the `tracked` one is corrected from the
    measured peaks by a root find)."""

    name = "expression"
    label = "Expression  (your formula)"
    help = ("A formula over named inputs (read at each point) and parameters\n"
            "(numbers). Allowed: numbers, names, + - * / **, sqrt abs sin cos\n"
            "exp log (radians), pi. A NaN or a math error = 'not valid here'.")
    options = (
        Option("formula", "formula", (), "a + b*x",
               help="e.g. 0.028*sqrt((B+hk)*(B+hk+meff))"),
        Option("unit", "result unit", (), "",
               help="Unit of the formula's result (e.g. GHz); empty = the detector's axis unit."),
        Option("tracked", "track", (), "",
               help="The parameter corrected from every clean peak (empty = none)."),
    )

    def formula(self, options) -> Formula:
        return Formula(options.get("formula", ""))

    def output_unit_for(self, options):
        return (options.get("unit") or "").strip()

    def tracked_for(self, options):
        return (options.get("tracked") or "").strip() or None

    # The formula's free names, split by the RECIPE: a name the block's
    # `inputs` maps is an input, every other name a parameter.
    def _names(self, w) -> list[str]:
        try:
            return self.formula(w.get("options") or {}).names
        except FormulaError:
            return []

    def inputs_for(self, w):
        mapped = w.get("inputs") or {}
        return [Input(n, n) for n in self._names(w) if n in mapped]

    def params_for(self, w):
        mapped = w.get("inputs") or {}
        return [Param(n, n, "", None) for n in self._names(w) if n not in mapped]

    def predict(self, x, p, options):
        try:
            f = self.formula(options)
        except FormulaError:
            return math.nan
        return f({**p, **x})

    def validate(self, options):
        errs = []
        try:
            self.formula(options)
        except FormulaError as exc:
            errs.append(f"window formula: {exc}")
        return errs


register(Kittel())
register(Linear())
register(Expression())


__all__ = ["Predictor", "Input", "Param", "Option", "register", "get", "names",
           "all_predictors", "Formula", "FormulaError", "solve_for",
           "output_factor", "unit_text", "INPUT_FAMILIES", "FUNCTIONS",
           "plugin_errors", "LAB_DIR"]
