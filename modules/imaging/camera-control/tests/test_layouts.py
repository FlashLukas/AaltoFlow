"""The Camera tab's switchable layouts (Lukas, 2026-10-13: "can you build A, C,
D, E and allow switching between them").

Six layouts -- Classic, Cockpit, Workflow, Heads-up, Split by axis, Image
first (2026-10-14, "build B as well") -- of ONE
set of controls. A switch re-parents the same widget objects into new
containers (apps/layouts.py). What is checked here:

* every control exists in every layout, is the SAME object as in Classic, and
  can be reached (visible, or one tab / step / pop-up away);
* switching many times leaves no orphaned or duplicated widgets and the
  controls still work (Z+ steps Z on the brain, Allow tracking calls
  set_tracking);
* the choice is cfg.ui.layout: INI and wire round trip, restored at start,
  selectable from the Layout box and the Appearance tab;
* the Workflow lights follow the status;
* the panels on the image (Heads-up) do not take a click-to-go click;
* Image first: each control in ONE place (toolbar, Overlays panel or drawer),
  the drawer folds and opens, keeps everything reachable and is restored at
  start, the image width at full HD, a 1366 px screen;
* a remote window, holder and viewer, in every layout (the viewer guard still
  covers the moved controls). Ports 15802/15803.
"""

from __future__ import annotations

import os
import time
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication,  # noqa: E402
                               QCheckBox, QLabel, QWidget)

from camera.apps import layouts as L  # noqa: E402
from camera.apps.gui import MainWindow  # noqa: E402
from camera.config import LAYOUTS, Config, load_config, save_config  # noqa: E402
from camera.net import protocol  # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402

ALL = ("classic", "cockpit", "workflow", "hud", "split", "image_first")
#: hidden by the code that owns them until there is something to show
OWNER_HIDDEN = {"fault_bar", "lab_drive"}


def _pump(app, s=0.15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < s:
        app.processEvents()
        time.sleep(0.01)


def _flush(app):
    """Run the deferred deletes of torn-down containers."""
    app.processEvents()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture
def win():
    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, *_ = build_sim_system(cfg)
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    w.resize(1600, 1000)
    w.show()
    _pump(app)
    yield w, brain, cam, app
    w.close()
    brain.shutdown()
    _flush(app)


def test_the_layout_list_matches_the_config():
    assert tuple(L.LAYOUT_TITLES) == LAYOUTS == ALL
    assert set(L.BUILDERS) == set(ALL)


def test_every_layout_holds_the_same_controls_and_each_can_be_reached(win):
    w, brain, cam, app = win
    assert w.layouts.name == "classic"                     # the default
    classic = {n: id(getattr(w, n)) for n in L.PIECES}
    for name in ALL:
        assert w.set_layout(name, persist=False) == name
        _pump(app, 0.05)
        optional = L.OPTIONAL.get(name, set())
        for n in L.PIECES:
            p = getattr(w, n)
            assert id(p) == classic[n], (name, n)          # never rebuilt
            assert p.window() is w, (name, n)
            if n in optional:
                assert not w.layouts.placed(p) and not p.isVisibleTo(w), (name, n)
                continue
            assert w.layouts.placed(p), (name, n)
            w.layouts.reveal(p)
            app.processEvents()
            assert p.parentWidget().isVisibleTo(w), (name, n)
            if n not in OWNER_HIDDEN:
                assert p.isVisibleTo(w), (name, n)
    # the owner's own hiding survives the moves (nothing to show yet)
    assert w.fault_bar.isHidden() and w.lab_drive.isHidden()


def test_switching_many_times_leaves_no_orphans_and_the_controls_still_work(win):
    w, brain, cam, app = win
    for name in ALL:                                     # every layout built once
        w.set_layout(name, persist=False)
    w.set_layout("classic", persist=False)
    _flush(app)
    kinds = (QAbstractButton, QAbstractSpinBox, QCheckBox, QLabel, QWidget)
    counts = {k: len(w.findChildren(k)) for k in kinds}
    tops = len(QApplication.topLevelWidgets())
    for _ in range(4):
        for name in ALL + ("classic",):
            w.set_layout(name, persist=False)
    _flush(app)
    assert {k: len(w.findChildren(k)) for k in kinds} == counts
    assert len(QApplication.topLevelWidgets()) == tops
    # each control has exactly one parent chain into the window: the parking
    # widget is empty again apart from the Classic layout's unused pieces
    parked = [n for n in L.PIECES
              if getattr(w, n).parentWidget() is w.layouts.parking]
    assert parked == sorted(L.OPTIONAL["classic"])

    brain.set_z(5.0)                                     # a known Z target to step from
    calls = []
    real = brain.set_tracking
    brain.set_tracking = lambda on: calls.append(on) or real(on)
    for name in ALL:
        w.set_layout(name, persist=False)
        w.layouts.reveal(w.b_z_up)
        app.processEvents()
        start = brain._z_target
        w.z_step.setValue(0.5)
        w.b_z_up.click()
        assert brain._z_target == pytest.approx(start + 0.5), name
        w.chk_track.click()                              # a user click toggles it
        assert calls and calls[-1] == w.chk_track.isChecked(), name
    assert len(calls) == len(ALL)


def test_layout_is_saved_in_the_ui_group_ini_and_wire(tmp_path):
    cfg = Config()
    assert cfg.ui.layout == "classic"
    cfg.ui.layout = "hud"
    path = tmp_path / "camera.ini"
    save_config(cfg, str(path))
    assert load_config(str(path)).ui.layout == "hud"
    text = path.read_text(encoding="utf-8").replace("layout = hud", "layout = nonsense")
    path.write_text(text, encoding="utf-8")
    assert load_config(str(path)).ui.layout == "classic"    # an unknown one: the default
    d = protocol.config_to_dict(cfg)
    assert d["ui"]["layout"] == "hud"
    other = Config()
    protocol.apply_config_dict(other, {"ui": {"layout": "split"}})
    assert other.ui.layout == "split"


def test_the_window_restores_and_saves_the_layout(win):
    w, brain, cam, app = win
    # the Layout box: switches live and writes ui.layout into the camera's config
    i = w.cmb_layout.findData("cockpit")
    w.cmb_layout.setCurrentIndex(i)
    assert w.layouts.name == "cockpit" and brain.cfg.ui.layout == "cockpit"
    # the Appearance tab's form shows it, and an Apply there switches too
    form = w._form_widgets["ui"]["layout"]
    assert form.currentText() == "cockpit"
    form.setCurrentText("split")
    w._apply_settings([("UI", brain.cfg.ui)])
    assert w.layouts.name == "split" and brain.cfg.ui.layout == "split"
    assert w.cmb_layout.currentData() == "split"
    # a new window starts in the saved layout
    w2 = MainWindow(brain, brain.cfg, remote=False)
    try:
        assert w2.layouts.name == "split" and w2.cmb_layout.currentData() == "split"
    finally:
        w2.close()


def _fake(**kw):
    base = dict(af_running=False, af_id=0, af_error="OK", best_focus_v=1.0, z_unit="um",
                spot_calibrated=False, tracking_on=False, match_found=False,
                match_score=0.0, backups_n=0, stabilize_on=False, stable=False,
                distance_um=0.0, laser_goto=False, laser_settled=False,
                point_settled=False, spot_x=10.0, spot_y=20.0, z_voltage=3.0,
                xy_step_unit="um", stage_x=1.0, stage_y=2.0, stage_steps_x=0,
                stage_steps_y=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_workflow_lights_follow_the_status(win):
    w, brain, cam, app = win
    st = L.workflow_states(_fake())
    assert {k: v[0] for k, v in st.items()} == {"focus": "off", "spot": "off",
                                                "pattern": "off", "lock": "off",
                                                "place": "off"}
    st = L.workflow_states(_fake(af_id=3, spot_calibrated=True, tracking_on=True,
                                 match_found=True, match_score=0.9, backups_n=2,
                                 stabilize_on=True, stable=True, point_settled=True))
    assert {k: v[0] for k, v in st.items()} == {"focus": "ok", "spot": "ok",
                                                "pattern": "ok", "lock": "ok",
                                                "place": "ok"}
    assert "+2 backups" in st["pattern"][1]
    st = L.workflow_states(_fake(af_id=3, af_error="no peak", tracking_on=True,
                                 stabilize_on=True, laser_goto=True))
    assert st["focus"][0] == "bad" and st["pattern"][0] == "bad"
    assert st["lock"][0] == "busy" and st["place"] == ("busy", "placing the laser")
    assert L.workflow_states(_fake(af_running=True))["focus"][0] == "busy"

    # in the window: the stepper's lights take the same values
    w.set_layout("workflow", persist=False)
    stepper = w.layouts.stepper
    w._timer.stop()                          # only the fake status from here on
    w.layouts.update(_fake(spot_calibrated=True, tracking_on=True, match_found=False))
    assert stepper.leds["spot"].property("level") == "ok"
    assert stepper.leds["pattern"].property("level") == "bad"
    assert stepper.notes["pattern"].text() == "tracking on, no match"
    w.layouts.update(_fake(tracking_on=True, match_found=True, stabilize_on=True,
                           stable=True))
    assert stepper.leds["pattern"].property("level") == "ok"
    assert stepper.leds["lock"].property("level") == "ok"
    # a step shows ONLY its own controls
    stepper.buttons["lock"].click()
    app.processEvents()
    assert w.chk_stab.isVisibleTo(w) and not w.chk_track.isVisibleTo(w)
    stepper.buttons["pattern"].click()
    app.processEvents()
    assert w.chk_track.isVisibleTo(w) and not w.chk_stab.isVisibleTo(w)
    # the strip is always there
    for n in ("card_jog", "b_z_up", "b_z_dn", "b_kill"):
        assert getattr(w, n).isVisibleTo(w), n


def test_status_line_says_track_stable_af_z_xy():
    parts = L.status_parts(_fake(tracking_on=True, match_found=True, match_score=0.87,
                                 stabilize_on=True, stable=False, distance_um=0.42,
                                 af_id=1))
    text = " | ".join(t for _c, t in parts)
    assert text == ("match 0.87 | stabilising 0.42 um | AF OK | Z 3.00 um | "
                    "XY 1.00, 2.00 um")
    assert parts[0][0] == "ok" and parts[1][0] == "accent"


def test_heads_up_panels_do_not_take_a_click_to_go_click(win):
    w, brain, cam, app = win
    w.set_layout("hud", persist=False)
    for _ in range(5):
        w._refresh(); _pump(app, 0.03)
    clicks = []
    brain.click_to_go = lambda x, y: clicks.append((x, y))
    w.chk_click.setChecked(True)
    view = w.view
    centre = QPoint(view.width() // 2, view.height() // 2)
    assert view.childAt(centre) is None                  # no panel over the middle
    QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, centre)
    assert len(clicks) == 1
    # every panel sits inside the view and none overlaps another
    panels = [p for p in view.findChildren(L.HudPanel) if p.isVisible()]
    assert len(panels) >= 5
    rects = [p.geometry() for p in panels]
    for r in rects:
        assert view.rect().contains(r), r
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not a.intersects(b), (a, b)
    # the read-only status panel lets even its own clicks through
    status = w.status_strip.parentWidget()
    assert status.testAttribute(Qt.WA_TransparentForMouseEvents)
    # the controls ON the image are the window's own: Z+ there steps Z
    brain.set_z(5.0)
    start = brain._z_target
    w.z_step.setValue(0.25)
    QTest.mouseClick(w.b_z_up, Qt.LeftButton)
    assert brain._z_target == pytest.approx(start + 0.25)
    assert not clicks[1:]                                # ... and is no click-to-go
    # leaving the layout takes the panels off the image
    w.set_layout("classic", persist=False)
    _flush(app)
    assert not view.findChildren(L.HudPanel)


def test_a_small_screen_keeps_every_control_reachable(win):
    """1366 x 768: the side columns scroll, nothing is squeezed under its
    minimum width."""
    w, brain, cam, app = win
    w.resize(1366, 740)
    for name in ALL:
        w.set_layout(name, persist=False)
        _pump(app, 0.05)
        for n in L.PIECES:
            p = getattr(w, n)
            if not p.isVisible() or n in ("card_position", "card_laser"):
                continue                     # (fixed-width cards: their own choice)
            assert p.width() >= p.minimumSizeHint().width() - 1, (name, n)


# --------------------------------------------------------------------------- #
# a GUI on a service: holder and viewer, every layout
# --------------------------------------------------------------------------- #
CMD, PUB = 15802, 15803


def test_remote_windows_in_every_layout_keep_the_viewer_guard():
    pytest.importorskip("zmq")
    from camera.net.client import CameraClient
    from camera.net.service import CameraService

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    brain, *_ = build_sim_system(cfg)
    svc = CameraService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    made = []
    try:
        for i, nm in enumerate(("camera GUI A", "camera GUI B")):
            c = CameraClient("127.0.0.1", CMD, PUB, timeout_ms=3000, kind="gui", name=nm)
            c.identity["host"] = f"user@pc{i}"       # two PCs: B is a viewer
            c.start()
            made.append(c)
        a, b = made
        wa = MainWindow(a, Config(), remote=True)
        wb = MainWindow(b, Config(), remote=True)
        wa.show(); wb.show()
        _pump(app, 0.4)
        assert not wa._control_bar.viewer and wb._control_bar.viewer
        for name in ALL:
            wa.set_layout(name)                      # the holder: saved in the service
            assert brain.cfg.ui.layout == name
            wb.set_layout(name)                      # the viewer: this window only
            assert wb.layouts.name == name
            _pump(app, 0.1)
            bar = wb._control_bar
            # moved controls are still guarded; the always-usable ones are not
            assert bar.guarded_input(wb.b_af) is wb.b_af, name
            assert bar.guarded_input(wb.z_step) is wb.z_step, name
            assert bar.guarded_input(wb.b_kill) is None, name
            assert bar.guarded_input(wb.cmb_layout) is None, name
            assert bar.guarded_input(wa.b_af) is None, name     # another window
        # Image first's drawer: the holder's fold is kept in the service, the
        # viewer's in its own window only; the fold button works for a viewer
        wa.set_layout("image_first"); wb.set_layout("image_first")
        wa.layouts.drawer.btn.click()
        assert wa.layouts.drawer.folded and brain.cfg.ui.drawer_collapsed is True
        assert wb._control_bar.guarded_input(wb.layouts.drawer.btn) is None
        before, was = brain.cfg.ui.drawer_collapsed, wb.layouts.drawer.folded
        wb.layouts.drawer.btn.click()
        assert wb.layouts.drawer.folded is (not was)          # this window: folds
        assert brain.cfg.ui.drawer_collapsed is before       # the service: unchanged
        assert wb._control_bar.guarded_input(wb.b_af) is wb.b_af
        assert wb._control_bar.guarded_input(wb.chk_track) is wb.chk_track
        assert "this window only" in wb.log.toPlainText()
        wa.close(); wb.close()
    finally:
        for c in made:
            c.close()
        svc.stop()
        _flush(app)


# --------------------------------------------------------------------------- #
# Image first (B, 2026-10-14): toolbar, foldable drawer, status line
# --------------------------------------------------------------------------- #
TOOLBAR = ("chk_track", "led_match", "chk_stab", "led_stable", "chk_cont", "led_af",
           "chk_click", "b_af", "b_kill", "b_snap", "box_zoom")
OVERLAYS = ("chk_thr", "chk_spot_info", "chk_pat_info", "chk_points")


def _inside(widget, container) -> bool:
    p = widget
    while p is not None:
        if p is container:
            return True
        p = p.parentWidget()
    return False


def test_image_first_puts_each_control_in_one_place(win):
    w, brain, cam, app = win
    w.set_layout("image_first", persist=False)
    _pump(app, 0.1)
    drawer = w.layouts.drawer
    assert drawer is not None
    for n in TOOLBAR:                    # the toolbar's switches are NOT in the drawer
        p = getattr(w, n)
        assert not _inside(p, drawer), n
        assert p.isVisibleTo(w), n       # the toolbar is always there
    for n in OVERLAYS:                   # the drawing switches: the Overlays panel
        p = getattr(w, n)
        assert isinstance(p.parentWidget(), L.HudPanel), n
        assert not _inside(p, drawer), n
    in_drawer = set(L.PIECES) - set(TOOLBAR) - set(OVERLAYS) - {
        "view", "status_strip", "fault_bar"}
    for n in in_drawer:
        assert _inside(getattr(w, n), drawer), n
    # the status line under the image: the state, and the newest log line
    w._timer.stop()
    w.status_strip.update_status(_fake(tracking_on=True, match_found=True,
                                       match_score=0.91, stabilize_on=True,
                                       distance_um=0.3))
    assert "match&nbsp;0.91" in w.status_strip.text()
    w._log_event("info", "the newest line")
    app.processEvents()
    line = w.status_strip.parentWidget()
    assert isinstance(line, L.StatusLine) and line.last.text().endswith("the newest line")


def test_image_first_drawer_folds_keeps_everything_reachable_and_is_kept(win):
    w, brain, cam, app = win
    w.set_layout("image_first", persist=True)
    drawer = w.layouts.drawer
    assert not drawer.folded and brain.cfg.ui.drawer_collapsed is False
    _pump(app, 0.1)
    open_view = w.view.width()
    drawer.btn.click()                                   # the user folds it
    _pump(app, 0.1)
    assert drawer.folded and not drawer.body.isVisibleTo(w)
    assert brain.cfg.ui.drawer_collapsed is True         # kept in the camera's config
    assert w.view.width() > open_view                    # the image got the room
    assert drawer.width() < 60                           # a thin strip of tab names
    # clicking a tab name while folded opens the drawer on that page
    drawer.bar.tabBarClicked.emit(2)
    drawer.bar.setCurrentIndex(2)
    app.processEvents()
    assert not drawer.folded and brain.cfg.ui.drawer_collapsed is False
    assert w.card_jog.isVisibleTo(w)
    # folded, every control can still be reached (reveal opens the drawer)
    w.set_drawer_collapsed(True)
    for n in L.PIECES:
        p = getattr(w, n)
        w.layouts.reveal(p)
        app.processEvents()
        assert p.parentWidget().isVisibleTo(w), n
        if n not in OWNER_HIDDEN:
            assert p.isVisibleTo(w), n
        w.set_drawer_collapsed(True)
    # a new window starts folded; a switch away and back keeps it folded
    w2 = MainWindow(brain, brain.cfg, remote=False)
    try:
        assert w2.layouts.name == "image_first" and w2.layouts.drawer.folded
    finally:
        w2.close()
    w.set_layout("hud", persist=False)
    w.set_layout("image_first", persist=False)
    assert w.layouts.drawer.folded
    # the Appearance tab's field opens it again
    form = w._form_widgets["ui"]["drawer_collapsed"]
    form.setChecked(False)
    w._apply_settings([("UI", brain.cfg.ui)])
    assert not w.layouts.drawer.folded and brain.cfg.ui.drawer_collapsed is False


def test_image_first_drawer_state_survives_the_ini(tmp_path):
    cfg = Config()
    assert cfg.ui.drawer_collapsed is False
    cfg.ui.layout, cfg.ui.drawer_collapsed = "image_first", True
    path = tmp_path / "camera.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))
    assert back.ui.layout == "image_first" and back.ui.drawer_collapsed is True
    cfg.ui.drawer_collapsed = False                      # gotcha #3: "False" is not True
    save_config(cfg, str(path))
    assert load_config(str(path)).ui.drawer_collapsed is False
    assert protocol.config_to_dict(cfg)["ui"]["drawer_collapsed"] is False


def test_image_first_gives_the_image_the_width(win):
    """Full HD with the drawer folded: the image >= 70 % of the tab's width."""
    w, brain, cam, app = win
    w.resize(1920, 1080)
    w.set_layout("image_first", persist=False)
    w.set_drawer_collapsed(True, persist=False)
    _pump(app, 0.2)
    assert w.view.width() >= 0.70 * w.camera_host.width()
    w.set_drawer_collapsed(False, persist=False)
    _pump(app, 0.2)
    assert w.view.width() >= 0.55 * w.camera_host.width()     # open: still the most


def test_image_first_fits_a_small_screen(win):
    """1366 x 768: the toolbar wraps instead of widening the window, the
    status line breaks between its parts, nothing is under its minimum."""
    w, brain, cam, app = win
    w.resize(1366, 740)
    w.set_layout("image_first", persist=False)
    tool = w.b_af.parentWidget()                 # the toolbar (a flow layout)
    assert isinstance(tool.layout(), L.FlowLayout)
    for folded in (False, True):
        w.set_drawer_collapsed(folded, persist=False)
        for page in range(w.layouts.drawer.stack.count()):
            w.layouts.drawer.bar.setCurrentIndex(page)
            _pump(app, 0.05)
            assert w.width() <= 1366 and w.camera_host.width() <= 1366, (folded, page)
            for n in TOOLBAR:                    # nothing of the toolbar cut off
                p = getattr(w, n)
                r = p.parentWidget().geometry() if n.startswith(("chk_", "led_")) \
                    else p.geometry()
                assert tool.rect().contains(r), (n, r, tool.rect())
            for n in L.PIECES:
                p = getattr(w, n)
                if not p.isVisible() or n in ("card_position", "card_laser"):
                    continue
                assert p.width() >= p.minimumSizeHint().width() - 1, (folded, page, n)
