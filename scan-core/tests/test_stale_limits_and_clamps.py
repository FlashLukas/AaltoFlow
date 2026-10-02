"""A scan must not run past a module's CURRENT limits, nor wait out a clamp.

Found on the rig (2026-10-02): a scan to camera.scan_ix 48 was validated
against the camera's OLD limits -- refreshing them gave up as soon as ANOTHER
connected module did not answer -- and the camera (48 points: 0..47) clamped
48 to 47. The adopt check waited for 48 and sat out its 60 s timeout:
"the measurement suite is stuck".
"""

from __future__ import annotations

import pytest

from scan_core import manifest as M
from scan_core.instrument import InstrumentError
from scan_core.lab import Lab
from scan_core.registry import Registry, Settable


class _Inst:
    def __init__(self, name, manifest, status=None, dead=False):
        self.name, self.alias, self.manifest = name, None, manifest
        self._status, self._dead, self.asked = status or {}, dead, []

    def status(self):
        if self._dead:
            raise InstrumentError(f"{self.name}: has not published a status for 9 s")
        return self._status

    def command(self, verb, **kw):
        self.asked.append(verb)
        return {"ok": True, "describe": self.fresh}


def _manifest(mod, rev, hi):
    return {"module": mod, "revision": rev,
            "parameters": [{"id": "scan_ix", "kind": "control", "type": "int",
                            "min": 0, "max": hi, "set": {"verb": "set_selected_index",
                                                         "arg": "ix"}}]}


def test_one_silent_module_does_not_stop_the_others_being_refreshed():
    lab = Lab()
    dead = _Inst("pm16", {"module": "pm16", "revision": 1, "parameters": []}, dead=True)
    cam = _Inst("camera", _manifest("camera", 10, 49), status={"describe_rev": 11})
    cam.fresh = _manifest("camera", 11, 47)
    lab.instruments = {"pm16": dead, "camera": cam}      # the dead one comes FIRST
    reg = Registry()
    reg.add(Settable("camera.scan_ix", "scan point X", "", (0, 49),
                     set_fn=lambda v: None, get_fn=lambda: 0))
    said = []
    moved = lab.refresh_stale(reg, prefix=True, on_warn=said.append)
    assert moved == ["camera.scan_ix"]
    assert reg.get("camera.scan_ix").limits == (0.0, 47.0)
    assert any("pm16" in m and "stale" in m for m in said)


def test_a_clamped_setpoint_fails_fast_and_says_why(monkeypatch):
    monkeypatch.setattr(M, "CLAMP_CHECK_S", 0.0)
    desc = {"id": "scan_ix", "settle": {"policy": "adopt_then_flag",
                                        "setpoint_key": "selected_index_x",
                                        "flag_key": "point_settled"}}
    inst = _Inst("camera", _manifest("camera", 10, 49))
    inst.fresh = _manifest("camera", 11, 47)
    guarded = M._clamp_guard(lambda st: False, inst, desc, 48, "camera.scan_ix", False)
    st = {"selected_index_x": 47, "point_settled": True}
    assert guarded(st) is False                  # first sighting: start the clock
    with pytest.raises(InstrumentError, match=r"clamped camera.scan_ix 48 -> 47.*\[0, 47\]"):
        guarded(st)
    assert inst.asked == ["describe"]            # re-read once


def test_a_slow_but_legal_setpoint_is_waited_for_as_before(monkeypatch):
    monkeypatch.setattr(M, "CLAMP_CHECK_S", 0.0)
    desc = {"id": "scan_ix", "settle": {"policy": "adopt_then_flag",
                                        "setpoint_key": "selected_index_x",
                                        "flag_key": "point_settled"}}
    inst = _Inst("camera", _manifest("camera", 10, 49))
    inst.fresh = _manifest("camera", 10, 49)     # 48 is legal: just not there yet
    guarded = M._clamp_guard(lambda st: st["selected_index_x"] == 48, inst, desc, 48,
                             "camera.scan_ix", False)
    assert guarded({"selected_index_x": 46}) is False
    assert guarded({"selected_index_x": 46}) is False   # checked once, no raise
    assert guarded({"selected_index_x": 48}) is True
    assert inst.asked == ["describe"]
