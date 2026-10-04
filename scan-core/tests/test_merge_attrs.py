"""Run info / provenance (snapshots branch) and the routine steps' comments
(routine-steps branch) share ONE ds_attrs dict. When the two branches were
merged on 2026-10-04 the engine's context briefly defined "ds_attrs" twice,
and the second (empty) one would have silently dropped the run info."""

import json

from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import build_sim_registry


def test_run_info_and_routine_comments_both_reach_the_file():
    reg = build_sim_registry()
    r = Recipe(name="t", axes=[{"type": "linear", "param": "field",
                                "start": 0, "stop": 10, "num": 3}],
               detectors=["lockin_r"],
               hooks=[{"when": "before_scan", "action": "call",
                       "args": {"steps": [{"comment": {"text": "hello"}}]}}])
    ds = run(r, reg, attrs={"sample": "B7", "operator": "someone"})
    assert ds.attrs["sample"] == "B7" and ds.attrs["operator"] == "someone"
    assert [c["text"] for c in json.loads(ds.attrs["comments"])] == ["hello"]
    assert "software_python" in ds.attrs            # provenance survived too
