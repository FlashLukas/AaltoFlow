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


def test_another_reader_of_describe_does_not_hide_the_change_from_the_registry():
    """The Control tab (and the clamp guard) store a freshly read describe as
    inst.manifest when the revision moves. refresh_stale compared with THAT
    copy, skipped, and the registry kept the old limits: the Scan tab offered
    scan point 0..48 for a 45-point array (roadmap bug, 2026-10-02)."""
    lab = Lab()
    cam = _Inst("camera", _manifest("camera", 10, 48), status={"describe_rev": 11})
    cam.limits_rev = 10                         # what build_lab_registry recorded
    cam.fresh = _manifest("camera", 11, 44)
    lab.instruments = {"camera": cam}
    reg = Registry()
    reg.add(Settable("camera.scan_ix", "scan point X", "", (0, 48),
                     set_fn=lambda v: None, get_fn=lambda: 0))
    cam.manifest = cam.fresh                    # the Control tab got there first
    assert lab.refresh_stale(reg, prefix=True) == ["camera.scan_ix"]
    assert reg.get("camera.scan_ix").limits == (0.0, 44.0)
    assert cam.limits_rev == 11
    assert lab.refresh_stale(reg, prefix=True) == []       # and then it is current


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


def test_an_echo_that_flaps_back_to_the_old_value_does_not_crash():
    """Lab 2026-10-07: an AFG phase scan died at its 2nd point with "unsupported
    operand type(s) for -: 'float' and 'NoneType'". The echo went old, new
    (= the request, but the flag not yet set), old again (a stale frame): the
    guard had kept `seen` = old while clearing `since`."""
    desc = {"id": "ch1_phase", "settle": {"policy": "adopt_then_flag",
                                          "setpoint_key": "ch1_phase_deg",
                                          "flag_key": "ch1_settled"}}
    inst = _Inst("afg", _manifest("afg", 1, 360))
    inst.fresh = _manifest("afg", 1, 360)
    guarded = M._clamp_guard(lambda st: st["ch1_phase_deg"] == 2.0225 and st["ch1_settled"],
                             inst, desc, 2.0225, "afg.ch1_phase", False)
    old = {"ch1_phase_deg": 0.0, "ch1_settled": True}
    new = {"ch1_phase_deg": 2.0225, "ch1_settled": False}
    for st in (old, new, old, old, new):
        assert guarded(st) is False
    assert guarded({"ch1_phase_deg": 2.0225, "ch1_settled": True}) is True


def _vna(rev, extras):
    """A VNA manifest: the primary trace `s` plus one per extra S-parameter."""
    params = [{"id": "s", "label": "S-parameter (S11)", "kind": "indicator",
               "type": "float", "read_path": ["s"]}]
    params += [{"id": f"s_{sp.lower()}", "label": f"S-parameter ({sp})",
                "kind": "indicator", "type": "float", "read_path": [f"s_{sp.lower()}"]}
               for sp in extras]
    return {"module": "vna", "revision": rev, "parameters": params}


def test_a_new_detector_appears_and_a_dropped_one_goes():
    """Blue Box, 2026-11-10: S21 ticked next to S11 in the VNA's GUI while the
    suite was connected -- the Scan tab kept showing S11 only. A new revision
    can ADD parameters; refresh_stale registers them (and says so), and drops
    the detectors the module no longer offers."""
    lab = Lab()
    vna = _Inst("vna", _vna(1, []), status={"describe_rev": 2})
    vna.limits_rev = 1
    vna.fresh = _vna(2, ["S21"])
    lab.instruments = {"vna": vna}
    reg = Registry()
    M.register_manifest(reg, vna, vna.manifest, prefix=True)
    reg.owner = {"vna.s": "vna"}
    seen = []
    lab.refresh_stale(reg, prefix=True, on_params=lambda a, r: seen.append((a, r)))
    assert reg.get("vna.s_s21") is not None and reg.owner["vna.s_s21"] == "vna"
    assert seen == [(["vna.s_s21"], [])]
    vna._status = {"describe_rev": 3}                 # S21 unticked again
    vna.fresh = _vna(3, [])
    lab.refresh_stale(reg, prefix=True, on_params=lambda a, r: seen.append((a, r)))
    assert reg.get("vna.s_s21") is None and seen[-1] == ([], ["vna.s_s21"])
    assert reg.get("vna.s") is not None


def test_the_scan_tab_keeps_its_ticks_when_detectors_are_added():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apps.scan_builder import ScanBuilder
    vna = _Inst("vna", _vna(1, []))
    reg = Registry()
    M.register_manifest(reg, vna, vna.manifest, prefix=True)
    b = ScanBuilder(registry=reg, embedded=True)
    ids = [it.data(0, QtCore.Qt.UserRole) for it in b._det_items()]
    assert ids == ["vna.s"]
    M.register_manifest(reg, vna, _vna(2, ["S21"]), prefix=True)   # what refresh adds
    b.reload_parameters()
    ticked = {it.data(0, QtCore.Qt.UserRole): it.checkState(0) == QtCore.Qt.Checked
              for it in b._det_items()}
    assert set(ticked) == {"vna.s", "vna.s_s21"} and ticked["vna.s"] is True
