"""A control's declared RESOLUTION: setpoints are rounded to it before they
are sent, and the settle waits for the ROUNDED value.

Lab PC 2026-10-06: the DS SG12000L's attenuator moves in 0.5 dB steps and
its firmware IGNORES an off-step request. A 5-point -20..5 dBm axis asked for
-13.75; the unit stayed at -20 and the point waited its full 60 s. `step` in a
descriptor is only a GUI increment (clMag's 2 mT field step is no
quantisation), so the hardware fact is its own key: `resolution`.
"""

from __future__ import annotations

import copy

from conftest import DEMO_MANIFEST

from scan_core.lab import build_lab_registry
from scan_core.preview import preview_axis


def _manifest(resolution=0.5):
    m = copy.deepcopy(DEMO_MANIFEST)
    for d in m["parameters"]:
        if d["id"] == "rf_power":
            d["resolution"] = resolution
            d["step"] = 0.25               # a GUI increment: must NOT be used
    return m


def test_a_setpoint_is_rounded_to_the_resolution_and_settles(fake_service):
    svc = fake_service(16950, manifest=_manifest())
    reg, lab = build_lab_registry(host="127.0.0.1", include=("fake",),
                                  ports={"fake": svc.cmd_port}, prefix=True)
    try:
        p = reg.get("fake.rf_power")
        assert p.resolution == 0.5
        p.set(-13.75)                     # would time out if -13.75 were waited for
        assert svc._rf_power == -14.0
        p.set(-7.3)
        assert svc._rf_power == -7.5
    finally:
        lab.close()


def test_without_a_resolution_nothing_is_rounded(fake_service):
    svc = fake_service(16952, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("fake",),
                                  ports={"fake": svc.cmd_port}, prefix=True)
    try:
        reg.get("fake.rf_power").set(-13.75)
        assert svc._rf_power == -13.75
    finally:
        lab.close()


def test_the_axis_preview_shows_the_rounding(fake_service):
    svc = fake_service(16954, manifest=_manifest())
    reg, lab = build_lab_registry(host="127.0.0.1", include=("fake",),
                                  ports={"fake": svc.cmd_port}, prefix=True)
    try:
        (dim,) = preview_axis({"type": "linear", "param": "fake.rf_power",
                               "start": -20, "stop": 5, "num": 5}, reg)
        m = dim.members[0]
        assert list(m.sent) == [-20.0, -14.0, -7.5, -1.0, 5.0]
        assert "resolution 0.5" in m.notes[1] and m.notes[0] == ""
    finally:
        lab.close()
