"""snapshot.py and run_info.py on their own: no Qt, no services."""

from __future__ import annotations

import json

import pytest

from scan_core import run_info
from scan_core.snapshot import (MISSING, MAX_ELEMENTS, all_settings, bounded,
                                diff_config, instrument_snapshot,
                                partial_config, provenance, read_snapshot,
                                recallable, same_value, snapshot_attrs,
                                take_snapshot)


# ---- bounding ----------------------------------------------------------------

def test_huge_arrays_and_texts_are_replaced_by_their_size():
    st = {"trace": list(range(MAX_ELEMENTS + 1)), "small": [1, 2, 3],
          "frame": "x" * 10000, "nested": {"m": [[0] * 600, [0] * 600]},
          "bad": float("nan"), "inf": float("-inf")}
    b = bounded(st)
    assert b["trace"] == f"<array n={MAX_ELEMENTS + 1}>"
    assert b["small"] == [1, 2, 3]
    assert b["frame"] == "<text n=10000>"
    assert b["nested"]["m"] == "<array n=1200>"      # leaves are counted
    assert b["bad"] == "nan" and b["inf"] == "-inf"
    json.dumps(b, allow_nan=False)                      # strict JSON


def test_identity_keys_can_be_dropped():
    assert bounded({"idn": "X,SN1", "a": {"serial": 3, "b": 1}},
                   drop_keys={"idn", "serial"}) == {"a": {"b": 1}}


# ---- diff ------------------------------------------------------------------

SAVED = {"motion": {"speed": 1.5, "preset": "slow", "steps": [1, 2, 3],
                    "deep": {"x": 1.0}},
         "ui": {"theme": "dark", "on": True},
         "gone": {"old": 4}}


def test_diff_finds_only_real_differences():
    cur = {"motion": {"speed": 1.5 * (1 + 1e-15), "preset": "fast",
                      "steps": [1, 2, 4], "deep": {"x": 2.0}},
           "ui": {"theme": "dark", "on": True, "new": 7}}
    d = {p: (a, b) for p, a, b in diff_config(SAVED, cur)}
    assert ("motion", "speed") not in d                  # float within 1e-12
    assert d[("motion", "preset")] == ("slow", "fast")
    assert d[("motion", "steps")] == ([1, 2, 3], [1, 2, 4])  # list = one setting
    assert d[("motion", "deep", "x")] == (1.0, 2.0)      # nested groups walked
    assert d[("gone", "old")] == (4, MISSING)            # only in the file
    assert d[("ui", "new")] == (MISSING, 7)              # only live now
    assert ("ui", "theme") not in d
    assert len(all_settings(SAVED, cur)) > len(d)


def test_type_changes_are_differences():
    assert not same_value(True, 1)
    assert not same_value(1, "1")
    assert same_value(1, 1.0)
    assert not same_value(1.0, 1.0 + 1e-9)
    assert same_value([1.0, "a"], (1.0, "a"))
    assert not same_value([1], [1, 2])
    d = diff_config({"g": {"k": 1}}, {"g": {"k": True}})
    assert d == [(("g", "k"), 1, True)]


def test_recallable_needs_both_sides_and_a_real_value():
    assert recallable(1, 2)
    assert not recallable(MISSING, 2)
    assert not recallable(1, MISSING)
    assert not recallable("<array n=5000>", [1])


def test_partial_config_holds_only_the_ticked_keys():
    part = partial_config([("motion", "preset"), ("motion", "deep", "x"),
                           ("ui", "on")], SAVED)
    assert part == {"motion": {"preset": "slow", "deep": {"x": 1.0}},
                    "ui": {"on": True}}
    with pytest.raises(KeyError):
        partial_config([("motion", "nope")], SAVED)


# ---- attributes ------------------------------------------------------------

def test_attrs_round_trip_and_bad_json_is_reported():
    snap = {"kim": {"module": "kim", "config": SAVED},
            "hf2_lab2": {"module": "hf2", "error": "get_config: timeout"}}
    attrs = snapshot_attrs(snap, when="2026-10-04T10:00:00")
    assert attrs["snapshot_modules"] == "kim,hf2_lab2"
    assert attrs["snapshot_time"] == "2026-10-04T10:00:00"
    attrs["snapshot_end"] = "{}"
    attrs["snapshot_broken"] = "{not json"
    back = read_snapshot(attrs)
    assert back["kim"]["config"] == SAVED
    assert back["hf2_lab2"]["error"].startswith("get_config")
    assert "error" in back["broken"]
    assert set(back) == {"kim", "hf2_lab2", "broken"}   # reserved keys skipped
    assert read_snapshot({"name": "old file"}) == {}


class _Inst:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.alias = name, fail, None
        self.manifest = {"module": name, "revision": 3, "label": name.upper()}

    def command(self, verb, _timeout_ms=None, **kw):
        if self.fail:
            raise RuntimeError("no reply")
        if verb == "get_config":
            return {"ok": True, "config": {"g": {"k": 1, "serial": "SN9"}}}
        if verb == "info":
            return {"ok": True, "info": {"idn": "ACME,1,SN9", "max": 5}}
        raise AssertionError(verb)

    def status(self):
        if self.fail:
            raise RuntimeError("silent")
        return {"x": 1.0, "control": {"holder": {"host": "SOMEPC"}},
                "clients": [{"name": "someone"}], "idn": "ACME"}


class _Lab:
    def __init__(self, *insts):
        self.instruments = {i.name: i for i in insts}


def test_take_snapshot_never_raises_and_drops_session_and_identity():
    snap = take_snapshot(_Lab(_Inst("kim"), _Inst("dead", fail=True)),
                         include_idn=False)
    k = snap["kim"]
    assert k["module"] == "kim" and k["revision"] == 3
    assert k["config"] == {"g": {"k": 1}}               # serial dropped
    assert "control" not in k["status"] and "clients" not in k["status"]
    assert "idn" not in k["status"] and "idn" not in k["info"]
    assert "error" in snap["dead"] and "get_config" in snap["dead"]["error"]
    with_idn = instrument_snapshot(_Inst("kim"), include_idn=True)
    assert with_idn["info"]["idn"] == "ACME,1,SN9"
    assert "control" not in with_idn["status"]           # never, idn or not


def test_provenance_has_the_python_version():
    p = provenance()
    assert p["software_python"].count(".") == 2
    assert all(isinstance(v, str) and v for v in p.values())


# ---- run info --------------------------------------------------------------

def test_run_info_normalises_and_omits_empty_fields():
    vals = run_info.normalise({"sample": "  YIG   B ", "tags": "fmr, ,yig,  fmr ,x  y",
                               "comment": " two\nlines "})
    assert vals["sample"] == "YIG B"
    assert vals["tags"] == "fmr, yig, x y"
    assert vals["comment"] == "two\nlines"
    assert vals["operator"] == ""
    attrs = run_info.run_info_attrs(vals)
    assert attrs == {"sample": "YIG B", "tags": "fmr, yig, x y"}   # no comment


def test_run_info_persists_in_the_settings_file(tmp_path):
    run_info.save({"sample": "S1", "operator": "op", "tags": "a,b"}, root=tmp_path)
    again = run_info.load(root=tmp_path)
    assert again["sample"] == "S1" and again["tags"] == "a, b"
    assert again["project"] == ""
