"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b): an enum is stored as a code plus its option names, and a
value that is not one of the options is stored as "not measured" -- lost. So
the state enum must list EVERY state the controller can enter. The states are
read from the CODE (every `self._state = X` in controller.py), not from one
status snapshot, which would only ever show OFF.
"""

import re
from pathlib import Path

from mag2dcal import controller
from mag2dcal.backends.sim import FakeClock
from mag2dcal.config import Config
from mag2dcal.net.describe import build_manifest
from mag2dcal.sim_system import build_sim_system


def _by_id():
    cfg = Config()
    cfg.calibration.load_newest_on_start = False
    clock = FakeClock()
    ctrl, _ = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=1)
    return ctrl, {p["id"]: p for p in build_manifest(ctrl)["parameters"]}


def test_state_is_an_enum_covering_every_state_the_code_enters():
    ctrl, p = _by_id()
    assert p["state"]["type"] == "enum"
    opts = set(p["state"]["options"])
    text = Path(controller.__file__).read_text(encoding="utf-8")
    # every UPPER_CASE name on the right of `self._state = ...` (also both
    # arms of a conditional expression)
    names = set()
    for rhs in re.findall(r"self\._state\s*=\s*(.+)", text):
        names |= set(re.findall(r"\b([A-Z][A-Z_]+)\b", rhs))
    entered = {getattr(controller, n) for n in names}
    assert {"OFF", "SEEK", "HOLD", "STABLE", "CALIBRATE", "FAULT"} <= entered, \
        "the regex no longer finds the state assignments"
    assert entered <= opts, f"states the code enters but describe omits: {entered - opts}"
    assert ctrl.status().state in opts


def test_flags_are_bools_and_no_indicator_promises_an_int_range():
    _, p = _by_id()
    for flag in ("field_stable", "frozen", "calibrated", "water_ok",
                 "output", "stabilizer", "water_bypass"):
        assert p[flag]["type"] == "bool", flag
    assert not [d["id"] for d in p.values()
                if d["kind"] == "indicator" and d["type"] == "int"]
