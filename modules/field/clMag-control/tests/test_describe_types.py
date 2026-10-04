"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b): an enum is stored as a code plus its option names, and a
value that is not one of the options is stored as "not measured" -- lost. So
the state enum must list EVERY state the controller can enter. The states are
enumerated from the CODE (the State enum and every `State.X` the controller
uses), not from one status snapshot, which would only ever show IDLE.
"""

import re
from pathlib import Path

from clMag.config import Config
from clMag.controller import State
from clMag.sim_system import build_sim_system
from clMag.net.describe import build_manifest

SRC = Path(__file__).resolve().parents[1] / "src" / "clMag" / "controller.py"


def _by_id():
    ctrl, *_ = build_sim_system(Config())
    return ctrl, {p["id"]: p for p in build_manifest(ctrl)["parameters"]}


def test_state_is_an_enum_covering_every_state_the_code_enters():
    ctrl, p = _by_id()
    assert p["state"]["type"] == "enum"
    opts = set(p["state"]["options"])
    assert opts == {s.value for s in State}
    used = set(re.findall(r"State\.([A-Z_]+)", SRC.read_text(encoding="utf-8")))
    assert used <= opts, f"states the code uses but describe omits: {used - opts}"
    assert ctrl.status().state in opts


def test_flags_are_bools_and_no_indicator_promises_an_int_range():
    _, p = _by_id()
    for flag in ("field_stable", "output_on", "locked", "stabilizer"):
        assert p[flag]["type"] == "bool", flag
    # clMag reports no counters: an int indicator here would be a mistake
    assert not [d["id"] for d in p.values()
                if d["kind"] == "indicator" and d["type"] == "int"]
