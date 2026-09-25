"""The Navigator tab, offscreen, driving the SIMULATED stage.

The full loop a user does: open a design, put two features "under the laser"
(set the sim stage there), say "I am here" twice, click a third feature, Go --
and the stage must end up where that feature really is.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
gdstk = pytest.importorskip("gdstk")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtGui, QtWidgets

from apps.navigator import NavigatorWidget
from scan_core import build_sim_registry


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def design(tmp_path):
    lib = gdstk.Library()
    c = lib.new_cell("CHIP")
    for x, y in ((-60, -60), (60, -60), (60, 60), (-60, 60), (0, 0)):   # 5 markers
        c.add(gdstk.rectangle((x - 2, y - 2), (x + 2, y + 2), layer=1))
    p = tmp_path / "chip.gds"
    lib.write_gds(str(p))
    return p


# The sample sits on the stage rotated by 30 deg and shifted: this is the
# "truth" the navigator has to find from two reference points.
ROT, OFF = math.radians(30.0), (12.0, -7.0)


def true_stage(dx, dy):
    return (math.cos(ROT) * dx - math.sin(ROT) * dy + OFF[0],
            math.sin(ROT) * dx + math.cos(ROT) * dy + OFF[1])


def _put_stage(nav, xy):
    nav.pair.x.set(xy[0]); nav.pair.y.set(xy[1])


def _wait_idle(nav, qapp, timeout=5.0):
    t0 = time.time()
    while nav._moving and time.time() - t0 < timeout:
        qapp.processEvents(); time.sleep(0.01)
    assert not nav._moving


def test_register_two_points_then_go(qapp, design):
    logs = []
    nav = NavigatorWidget(on_log=logs.append)
    nav.set_source(registry=build_sim_registry())
    assert nav.pair.label == "simulator"
    assert nav.open_gds(design)
    assert list(nav.design.layers) == ["1/0"]

    for feature in ((-60, -60), (60, 60)):
        # the operator drives the stage until the feature is under the laser ...
        _put_stage(nav, true_stage(*feature))
        # ... then clicks that feature on the picture (in the picture's CURRENT
        # stage frame, which is what a click delivers)
        nav._on_click(*nav.reg.to_stage(*feature))
        assert nav.pick == pytest.approx(feature, abs=1e-6)
        assert nav.add_reference()

    s = nav.reg.summary()
    assert s["rotation_deg"] == pytest.approx(30.0, abs=1e-6)
    assert nav.rot_spin.value() == pytest.approx(30.0, abs=0.01)
    assert not nav.rot_spin.isEnabled()

    # click a third feature and go there
    nav._on_click(*nav.reg.to_stage(60, -60))
    assert nav.go()
    _wait_idle(nav, qapp)
    assert nav.stage_um() == pytest.approx(true_stage(60, -60), abs=1e-6)
    assert "navigator: arrived" in logs


def test_outside_the_travel_is_refused(qapp, design):
    logs = []
    nav = NavigatorWidget(on_log=logs.append)
    nav.set_source(registry=build_sim_registry())       # travel +-100 um
    nav.open_gds(design)
    _put_stage(nav, (0.0, 0.0))
    nav._on_click(0.0, 0.0)
    nav.add_reference()                                  # design (0,0) at stage (0,0)
    nav._on_click(60.0, 60.0)
    nav.approach.setValue(0)
    nav.reg.shift = (80.0, 0.0)                          # pushes (60,60) to x = 140
    nav._refresh_all()
    assert not nav.go()
    assert any("outside" in m for m in logs)


def test_offset_correction_and_session_round_trip(qapp, design, tmp_path):
    nav = NavigatorWidget()
    nav.set_source(registry=build_sim_registry())
    nav.open_gds(design)
    for feature in ((-60, -60), (60, 60)):
        _put_stage(nav, true_stage(*feature))
        nav._on_click(*nav.reg.to_stage(*feature))
        nav.add_reference()
    # the stage "drifted": the centre marker is found 5 um further in x
    x, y = true_stage(0, 0)
    _put_stage(nav, (x + 5.0, y))
    nav._on_click(*nav.reg.to_stage(0, 0))
    assert nav.correct_offset()
    assert nav.reg.to_stage(0, 0) == pytest.approx((x + 5.0, y), abs=1e-6)

    nav.fov_w.setValue(80); nav.fov_h.setValue(60); nav.approach.setValue(10)
    path = tmp_path / "chip.nav.json"
    nav.save(path)

    nav2 = NavigatorWidget()
    nav2.set_source(registry=build_sim_registry())
    assert nav2.load(path)
    assert nav2.reg.to_stage(60, -60) == pytest.approx(nav.reg.to_stage(60, -60))
    assert (nav2.fov_w.value(), nav2.approach.value()) == (80, 10)


def test_an_image_design_is_placed_by_its_real_width(qapp, tmp_path):
    img = QtGui.QImage(200, 100, QtGui.QImage.Format_RGB32)
    img.fill(0x336699)
    p = tmp_path / "photo.png"
    img.save(str(p))
    nav = NavigatorWidget()
    nav.set_source(registry=build_sim_registry())
    assert nav.open_image(p, 150.0)
    assert nav.design.bbox() == pytest.approx((-75, -37.5, 75, 37.5))
    # the drawn picture covers exactly that rectangle on the stage (no points,
    # rotation 0 -> design frame == stage frame)
    r = nav._design_group.mapRectToScene(nav._design_group.childrenBoundingRect())
    assert (r.left(), r.bottom(), r.width(), r.height()) == pytest.approx((-75, 37.5, 150, 75))
