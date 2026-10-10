"""Render a module's front panel to a PNG, offscreen, with no display.

The suite's GUIs are the documentation nobody reads until they see it, so every
module README shows its panel. Refreshing those by hand (open the GUI, arrange
it, alt-print-screen, crop) is tedious enough that the images go stale, which is
worse than having none. This does it reproducibly instead.

Run it from inside the project whose panel you want, because it imports that
project's package from that project's environment:

    cd modules/field/clMag-control
    uv run --extra gui python ../../../tools/render_panels.py clMag

    cd ../../../mission-control
    uv run python ../tools/render_panels.py mission-control

Or refresh everything at once from the repo root:

    python tools/render_all.py

How it works (the recipe from docs/DEVELOPER_NOTES.md section 9): force Qt's `offscreen`
platform so no window ever appears, then monkeypatch `QApplication.exec` -- the
call that would normally block forever -- to pump the event loop for a couple of
seconds and grab the top-level widget instead.

The pumping matters. These panels are driven by status-poll timers at 30-60 ms
and several indicators animate on their own ~33 ms timers, so a screenshot taken
immediately after construction catches empty readouts and half-painted widgets.
Each target also gets a `warm_up` that drives the simulator first, because a
panel showing a magnet at 0 mT with every lamp dark documents nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Must be set before Qt is imported, or it will try to find a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The offscreen plugin uses Qt's *basic* font database, which on Windows starts
# out empty -- so every label renders as a tofu box and the screenshot is
# useless while looking almost right. Point it at the system fonts.
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent
PANELS = ROOT / "front-panels"

#: Where rendered panels pretend to keep their data. Screenshots are published,
#: and a path shown in one (a data folder, a save label, a file list) must not
#: carry the user name of the PC that rendered it -- the renders of 2026-09-24
#: showed C:\Users\<lab account>\... in four panels. C:\Users\Public is
#: writable by every account and names nobody.
NEUTRAL = (Path(r"C:\Users\Public\Documents\AaltoFlow") if sys.platform == "win32"
           else Path("/tmp/AaltoFlow"))

#: Window size per panel. These are not arbitrary: each is the smallest size
#: that shows the whole panel without a scrollbar and without a lake of empty
#: space, which is what makes the images readable at README width.
SIZES = {
    "clMag": (1320, 900),
    "smb": (1280, 620),
    "afg": (1320, 820),
    "scope": (1560, 960),
    "scope-xy": (1560, 960),
    "scope-ad": (1560, 960),
    "control-holder": (1280, 660),
    "control-viewer": (1280, 660),
    "stage": (1280, 800),
    "piezo": (1240, 760),
    "camera": (1400, 940),
    "camera-settings": (1400, 940),
    "kim": (1240, 1240),
    "hf2": (1400, 900),
    "hf2-ch2": (1400, 900),
    "hf2-aux": (1400, 900),
    "hf2-instrument": (1400, 900),
    "pm16": (1180, 780),
    "ppms": (1180, 780),
    "vna": (1320, 900),
    "mag2d": (1320, 900),
    # Taller than mag2d: the sidebar gained the calibration card, and at 1020 the
    # fault card at its bottom was cut off.
    "mag2dcal": (1320, 1150),
    # Wider and taller since the conditions card joined the middle column: at
    # 1360 the axis row's "pts" box was clipped by the result pane.
    "scan-core": (1520, 840),
    "mission-control": (1180, 1150),
    "mission-control-instruments": (1240, 600),
    "mission-control-security-pc": (900, 600),
    "mission-control-security-pcs": (900, 600),
    "mission-control-security-policy": (900, 600),
    "suite-control": (1500, 950),
    "suite-control-dynacool": (1500, 950),
    "suite-scan": (1500, 950),
    "suite-measurement": (1500, 950),
    "suite-data": (1500, 950),
    "suite-settings": (1500, 1080),     # + the SCAN SERVER card (2026-10-05)
    "suite-catalogue": (1500, 760),
    "suite-navigator": (1500, 950),
    "suite-queue": (1500, 950),
    "suite-watch": (1500, 990),
    "suite-watch-remote": (1500, 1040),
    "suite-queue-dialog": (760, 430),
    "suite-fly-scan": (1500, 950),
    "suite-image-scan": (1500, 950),
    "suite-repeat-scan": (1500, 950),
    "suite-fly": (1500, 950),
    "suite-image": (1500, 950),
    "suite-scout-scan": (1500, 950),
    "suite-scout": (1500, 950),
    "suite-axis-advanced": (1500, 950),
    "suite-axis-advanced-scout": (1500, 950),
    "suite-axis-advanced-ramp": (1500, 950),
    "suite-routine-args": (1500, 950),
    "viewer-map": (1600, 960),
    "viewer-1d": (1600, 960),
}

#: Crop the grabbed image to this height, for panels whose minimum size is set
#: by their tallest tab and which therefore carry a lake of empty space below
#: the content on every other tab. The window really is that tall -- this trims
#: the picture, not the app -- but a README image that is 40% blank teaches less
#: than one that is not.
CROP_HEIGHT = {
    "camera": 1010,
}


# --------------------------- per-module targets -----------------------------
#
# Each target says how to build a simulated system, how to show it, and what to
# do to it first so the panel has something to display.

def _clMag(theme):
    from clMag.config import Config
    from clMag.sim_system import build_sim_system
    from clMag.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg)

    def warm_up(win):
        # Seek a real field: fills the live plot, lights the STABLE lamp and
        # makes the GMW3470 indicator glow.
        win.ctrl.set_field(40.0)

    return lambda: gui.run_app(ctrl, cfg, cal), warm_up, 6.0


def _smb(theme):
    from smb.config import Config
    from smb.sim_system import build_sim_system
    from smb.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg)

    def warm_up(win):
        # RF on at a healthy power, so the antenna indicator radiates.
        win.ctrl.set_frequency(2.45e9)
        win.ctrl.set_power(-3.0)
        win.ctrl.set_rf(True)

    return lambda: gui.run_app(ctrl, cfg), warm_up, 3.0


def _stage(theme):
    from stage.config import Config
    from stage.sim_system import build_sim_system
    from stage.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg)

    # stage, piezo, kim and camera all have their brain started by
    # scripts/run_gui.py, NOT by MainWindow (only clMag and smb do it in the
    # window). Their sim backends interpolate position from the wall clock, so
    # stage and kim *look* alive without it -- but piezo's software ramp runs on
    # a brain thread, so without start() its readout sits at 0.000 forever while
    # the log cheerfully reports the move. Do what run_gui.py does.
    ctrl.start()

    def warm_up(win):
        # Travel is 0..25 mm per axis, so a negative target just clamps to 0 and
        # the panel shows an axis that never moved.
        win.ctrl.move_axis(0, 12.0)
        win.ctrl.move_axis(1, 8.0)
        win.ctrl.move_axis(2, 3.0)

    return lambda: gui.run_app(ctrl, cfg), warm_up, 6.0


def _piezo(theme):
    from piezo.config import Config
    from piezo.sim_system import build_sim_system
    from piezo.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg)

    # stage, piezo, kim and camera all have their brain started by
    # scripts/run_gui.py, NOT by MainWindow (only clMag and smb do it in the
    # window). Their sim backends interpolate position from the wall clock, so
    # stage and kim *look* alive without it -- but piezo's software ramp runs on
    # a brain thread, so without start() its readout sits at 0.000 forever while
    # the log cheerfully reports the move. Do what run_gui.py does.
    ctrl.start()

    def warm_up(win):
        # Off-centre, so the XY travel map shows the marker away from home.
        win.ctrl.move_axis(0, 120.0)
        win.ctrl.move_axis(1, 60.0)

    # 120 um at the default 50 um/s software ramp takes ~3 s; wait it out so the
    # readout shows an arrived stage rather than one frozen at the start.
    return lambda: gui.run_app(ctrl, cfg), warm_up, 5.0


def _kim(theme):
    from kim.config import Config
    from kim.sim_system import build_sim_system
    from kim.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg)

    # stage, piezo, kim and camera all have their brain started by
    # scripts/run_gui.py, NOT by MainWindow (only clMag and smb do it in the
    # window). Their sim backends interpolate position from the wall clock, so
    # stage and kim *look* alive without it -- but piezo's software ramp runs on
    # a brain thread, so without start() its readout sits at 0.000 forever while
    # the log cheerfully reports the move. Do what run_gui.py does.
    ctrl.start()

    def warm_up(win):
        # Defaults are the SLOW + SMALL presets (300 steps/s), so an 18000-step
        # target is still crawling 60 s later. Fast preset + modest targets, so
        # the axes actually ARRIVE somewhere off-centre before the grab.
        win.ctrl.set_speed(True)
        win.ctrl.move_to_step(0, 4000)
        win.ctrl.move_to_step(1, -2000)
        win.ctrl.move_to_step(2, 1200)

    return lambda: gui.run_app(ctrl, cfg), warm_up, 4.5


def _camera(theme, tab: str | None = None):
    from camera.config import Config
    from camera.sim_system import build_sim_system
    from camera.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    built = build_sim_system(cfg)
    ctrl = built[0] if isinstance(built, tuple) else built

    # Same as stage/piezo/kim: run_gui.py starts the brain, not MainWindow.
    # Without this the vision engine never grabs a frame and the panel renders
    # an empty view saying "no frame" -- the one thing it exists to show.
    ctrl.start()

    def warm_up(win):
        # Tracking on so the overlays (spot crosshair, template box, scan grid)
        # are drawn -- they are the point of this panel.
        c = getattr(win, "ctrl", None)
        # 2026-10-10: a calibrated spot, a template and an AF position, so the
        # violet "AF" mark and the AF-position buttons are shown working
        try:
            c.calibrate_spot(10)
            cam = built[1]
            tcx, tcy = cam.template_center_px()
            c.capture_reference((tcx, tcy, 60, 60))
            c.set_af_position("um", x_um=-30.0, y_um=-40.0)
        except Exception:
            pass
        fn = getattr(c, "set_tracking", None)
        if fn:
            fn(True)
        if tab:
            # one tab by NAME (a settings tab: "Camera settings" holds the
            # Images for scans box, 2026-10-10)
            from PySide6 import QtWidgets
            for tw in win.findChildren(QtWidgets.QTabWidget):
                names = [tw.tabText(i) for i in range(tw.count())]
                if tab in names:
                    tw.setCurrentIndex(names.index(tab))
                    return
            raise SystemExit(f"camera: no tab named {tab!r}")

    return lambda: gui.run_app(ctrl, cfg), warm_up, 5.0


def _hf2_tab(tab: int):
    """One tab of the lock-in window (a tabbed window shows one at a time)."""
    def make(theme):
        from hf2.config import Config
        from hf2.sim_system import build_sim_system
        from hf2.apps import gui

        cfg = Config()
        cfg.ui.theme = theme
        ctrl, sim = build_sim_system(cfg, seed=7)
        sim.noise_V_rtHz = 2e-5          # a visible noise cloud on the phasor dial

        def warm_up(win):
            # one acquisition so the sample fields are filled; a step in ch1's
            # input so its plot shows the filter settling
            win.ctrl.set_time_constant(1, 0.03)
            sim.set_signal(0, 3.2e-3, 40.0)
            win.ctrl.acquire()
            win.tabs.setCurrentIndex(tab)      # after the changes: Instrument reloads on entry

        return lambda: gui.run_app(ctrl, cfg), warm_up, 6.0
    return make


def _scan_core(theme):
    sys.path.insert(0, str(ROOT / "scan-core"))
    from apps import scan_builder

    def show():
        # scan_builder.main() parses sys.argv directly and takes no argv
        # parameter, so hand it the flag that way.
        sys.argv = ["scan_builder", "--theme", theme]
        return scan_builder.main()

    def warm_up(win):
        # An empty builder documents nothing. Stack two axes and actually run
        # the scan, so the screenshot shows the thing worth seeing: a 2-D map
        # with the field-dependent resonance line curving across it.
        win.add_axis("field")
        win.add_axis("rf_freq")
        for row, (lo, hi, n) in zip(win.rows, [(0.0, 120.0, 41), (500.0, 2500.0, 81)]):
            row.start.setValue(lo)
            row.stop.setValue(hi)
            row.num.setValue(n)
        # ...and the conditions it was taken under, which are as much part of
        # the definition as the axes.
        win.name_edit.setText("fmr field-frequency map")
        win.name_edit.setCursorPosition(0)      # or the box shows its tail end
        win.add_fixed("rf_power", 8.0)
        win.add_fixed("device_v", 0.0)
        win.per_pt.setValue(0.0)        # ETA estimate only; the sim is instant
        win.run_scan(block=True)        # synchronous, so the plot is filled by
                                        # the time we grab

    return show, warm_up, 2.5


def _suite(tab: str, warm=None, settle=3.0, follow=False):
    """One tab of the measurement suite, addressed BY NAME.

    Each tab is its own render target rather than one shot of the window,
    because a tabbed app only ever shows one at a time and a README wants to
    show what each does.

    By name, not by index: adding the Data tab in the middle silently made the
    "settings" target render Data instead, and a screenshot has no test to fail.

    `follow` off by default, which matters more than it looks: the suite
    connects to whatever the launcher has running a fraction of a second AFTER
    the window opens, and adopting a new registry drops the axis stack. On a PC
    with services up, every scan panel came out saying "no axes" over a plot
    that had clearly just run one. Only the Control tab wants live modules.
    """
    def make(theme):
        sys.path.insert(0, str(ROOT / "scan-core"))
        from apps import suite as suite_mod

        holder = {}

        def show():
            sys.argv = ["suite", "--theme", theme]
            # Connect to whatever services happen to be running; the renderer
            # starts them beforehand. With none up it falls back to the
            # simulator rather than failing, which is also a fine screenshot.
            mods = os.environ.get("SUITE_RENDER_MODULES", "") if follow else ""
            if mods:
                sys.argv += ["--modules", mods]
            if not follow:
                sys.argv += ["--no-follow"]
            (NEUTRAL / "data").mkdir(parents=True, exist_ok=True)
            sys.argv += ["--out-dir", str(NEUTRAL / "data")]
            return suite_mod.main(sys.argv[1:])

        def warm_up(win):
            names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
            if tab not in names:
                raise SystemExit(f"no '{tab}' tab in the suite (have: {names})")
            win.tabs.setCurrentIndex(names.index(tab))
            if warm is not None:
                return warm(win)          # may hand back a dialog to photograph

        return show, warm_up, settle
    return make


def _tick_some_controls(win):
    """Tick a readable handful of parameters so the Control tab is not empty."""
    from PySide6 import QtWidgets, QtCore

    panel = win.control
    wanted_bits = ("field", "current", "state", "power", "frequency", "rf_on",
                   "position_x", "position_y", "closed_loop_x", "demag",
                   "measured_field", "aux_ai1")
    ticked = 0
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value() and ticked < 12:
        node = it.value()
        pid = node.data(0, QtCore.Qt.UserRole)
        if pid and any(pid.endswith(b) or pid.split(".")[-1] == b
                       for b in wanted_bits):
            node.setCheckState(0, QtCore.Qt.Checked)
            ticked += 1
        it += 1
    panel._rebuild_panel()


def _stack_two_axes(win):
    """Put a real measurement on the Scan tab: three nested axes (so the loop
    indentation shows) and the conditions it is held at."""
    b = win.builder
    b.name_edit.setText("fmr islands")
    if not b.rows:
        for pid, (start, stop, num) in (("rf_freq", (400, 1500, 12)),
                                        ("pos_y", (-45, 45, 61)),
                                        ("pos_x", (-45, 45, 61))):
            if b.registry.get(pid) is None:
                continue
            b.add_axis(pid)
            row = b.rows[-1]
            row.start.setValue(start); row.stop.setValue(stop); row.num.setValue(num)
    for pid, value in (("field", 40.0), ("rf_power", 8.0), ("device_v", 0.0)):
        if b.registry.get(pid) is not None:
            b.add_fixed(pid, value)
    # ...and the ROUTINES around it, as ordered steps: before the scan find
    # focus, then a reference at a far-off field (the field goes back to its
    # condition before the first point); afterwards the field to 0.
    if getattr(b, "routines", None) and b.registry.get("field") is not None:
        if hasattr(b, "add_routine_action"):          # several steps (2026-09-25)
            b.add_routine_action("before_scan", "sim_autofocus")
        b.add_routine_set("before_scan", "field", 190.0)
        if hasattr(b, "add_routine_action"):
            b.add_routine_action("before_scan", "vna_reference")
        else:
            b.set_routine_action("before_scan", "vna_reference")
        b.add_routine_set("after_scan", "field", 0.0)
    # ...and one THROUGHOUT: autofocus before every row of the map.
    if hasattr(b, "add_throughout") and b.registry.get_action("sim_autofocus"):
        b.add_throughout().set_action("sim_autofocus")
    b._rebuild_summary()


def _demo_chip_gds(path: Path) -> Path:
    """A made-up 4.6 mm test chip: a 4 x 4 dose matrix of dot arrays, a frame
    and alignment crosses. Made up on purpose -- a real sample design is
    somebody's unpublished work and does not belong in a public screenshot.
    """
    import gdstk
    lib = gdstk.Library()
    top = lib.new_cell("CHIP")
    frame = gdstk.boolean(gdstk.rectangle((-2300, -2300), (2300, 2300)),
                          gdstk.rectangle((-2250, -2250), (2250, 2250)), "not", layer=4)
    top.add(*frame)
    for cx, cy in ((-2100, -2100), (2100, -2100), (2100, 2100), (-2100, 2100)):
        top.add(gdstk.rectangle((cx - 60, cy - 6), (cx + 60, cy + 6), layer=4),
                gdstk.rectangle((cx - 6, cy - 60), (cx + 6, cy + 60), layer=4))
    pitch = 1000
    for i in range(4):
        for j in range(4):
            x0, y0 = -1950 + i * pitch, -1950 + j * pitch
            top.add(*gdstk.text(f"{50 * (4 * j + i + 1)}", 60, (x0, y0 + 820), layer=42))
            n = 3 + i + j                      # denser arrays at higher dose
            for a in range(n):
                for b in range(n):
                    r = 6 + 3 * ((a + b) % 3)
                    top.add(gdstk.ellipse((x0 + 80 + a * 640 / n, y0 + 80 + b * 640 / n), r,
                                          layer=43))
            top.add(gdstk.rectangle((x0 + 60, y0 + 740), (x0 + 700, y0 + 760), layer=62))
    lib.write_gds(str(path))
    return path


def _navigator_demo(win):
    """Navigator tab on a registered chip: two reference points, a selected
    target, the camera field of view -- all against the SIMULATED stage.

    NAV_RENDER_GDS=<file> renders a real design instead (for a look on this PC;
    do not commit that picture).
    """
    import math
    import tempfile
    nav = win.navigator
    p = nav.pair
    # The simulator's stage travel is +-100 um (its patterned sample); a chip
    # needs millimetres. Widened on these Settable objects only, for the picture.
    p.x.limits = p.y.limits = (-3000.0, 3000.0)
    gds = os.environ.get("NAV_RENDER_GDS") or str(
        _demo_chip_gds(Path(tempfile.mkdtemp()) / "demo_chip.gds"))
    nav.open_gds(gds)
    rot, off = math.radians(2.5), (180.0, -120.0)     # how the chip "really" sits

    def truth(x, y):
        return (math.cos(rot) * x - math.sin(rot) * y + off[0],
                math.sin(rot) * x + math.cos(rot) * y + off[1])

    for feat in ((-2100, -2100), (2100, 2100)):
        p.x.set(truth(*feat)[0]); p.y.set(truth(*feat)[1])
        nav._on_click(*nav.reg.to_stage(*feat))
        nav.add_reference()
    here = (-1450.0, -1100.0)
    p.x.set(truth(*here)[0]); p.y.set(truth(*here)[1])
    nav.fov_w.setValue(420); nav.fov_h.setValue(320)
    nav.approach.setValue(20)
    nav._on_click(*nav.reg.to_stage(550.0, 700.0))
    nav._poll()
    nav.fit_design()


def _stack_with_repeats(win):
    """The Scan tab with REPEAT rows (scan_core/repeat.py): three whole field
    sweeps kept (one every 10 minutes), every point averaged 10 times. Three
    rows, so both repeats fit the axis-stack card without scrolling."""
    win.use_simulator()
    b = win.builder
    b.name_edit.setText("field sweep, 3 runs, 10x averaged")
    b.name_edit.setCursorPosition(0)
    b.add_repeat(num=3, mode="keep", interval_s=600)
    b.add_axis("field")
    row = b.rows[-1]
    row.start.setValue(0.0); row.stop.setValue(120.0); row.num.setValue(41)
    b.add_repeat(num=10, mode="average")
    b.add_fixed("rf_freq", 1500.0)
    b.add_fixed("rf_power", 8.0)
    b.per_pt.setValue(0.05)


def _scan_then_show_run(win):
    """Run a scan the screenshot can actually wait for.

    The Measurement tab is rendered against the SIMULATOR on purpose. Two live
    axes at 21x21 is 441 real magnet settles -- minutes of waiting for a
    picture. The simulated registry produces the same panel with a real result
    in it, instantly.
    """
    win.use_simulator()
    b = win.builder
    b.name_edit.setText("fmr field-frequency map")
    b.name_edit.setCursorPosition(0)
    b.registry.get("rf_power").set(8.0)
    b.add_axis("field")
    b.rows[0].start.setValue(0.0); b.rows[0].stop.setValue(120.0)
    b.rows[0].num.setValue(41)
    b.add_axis("rf_freq")
    b.rows[1].start.setValue(500.0); b.rows[1].stop.setValue(2500.0)
    b.rows[1].num.setValue(61)
    b.per_pt.setValue(0.0)
    b.run_scan(block=True)


def _scout_scan(win):
    """The SCOUT PASS opened on the Scan tab (2026-10-08): an XY map at
    several frequencies, X and Y ticked 'scout' (every 3rd point), the
    reflectivity as what the scout looks at, a scout-only setting -- and the
    frequency OUTSIDE the scouted axes, so the 'outer axes' choice is live."""
    _stack_two_axes(win)
    b = win.builder
    for row in b.rows:
        if row.param.id in ("pos_x", "pos_y"):
            row.scout.setChecked(True)
            row.scout_step.setValue(3)
    sec = b.scout_section
    sec.det_box.setCurrentIndex(max(0, sec.det_box.findData("reflectivity")))
    if b.registry.get("rf_power") is not None:
        sec.add_scout_setting("rf_power", 12.0)
    sec.set_expanded(True)
    b._rebuild_summary()


def _axis_advanced(win):
    """The Advanced panel of an axis row opened in place (2026-10-09): the
    fly row of the fly pose, its FLY group filled in (speed, knob, zig-zag),
    the amber tags on the row, and the rows below pushed down."""
    _fly(run=False)(win)
    b = win.builder
    row = b.rows[1]
    row.timeout_auto.setChecked(False)
    row.timeout_spin.setValue(300.0)
    # one mean per row (2026-10-10): shown ticked, the pixels kept, so the
    # picture shows both boxes and the "row mean" tag
    row.collapse_box.setChecked(True)
    b.open_advanced(row)
    b._rebuild_summary()


def _routine_args(win):
    """A routine step's Advanced options (2026-10-10): the Scan pose, plus a
    THROUGHOUT routine "focus at the AF position at the start of each row"
    whose step has its gear open -- built from the action's declared args,
    two of them ticked (sent) and shown as tags on the step."""
    _stack_two_axes(win)
    b = win.builder
    for sec in list(getattr(b, "throughout", [])):
        b.remove_throughout(sec)
    sec = b.add_throughout()
    row = sec.add_action("sim_focus_at")
    if row is None:
        return
    for name, value in (("ix", 0), ("routine", "one_way")):
        tick, ed, _spec = row.args_panel.lines[name]
        tick.setChecked(True)
        if hasattr(ed, "setCurrentText"):
            ed.setCurrentText(value)
        else:
            ed.setValue(value)
    row.set_advanced_open(True)
    b._rebuild_summary()


def _axis_advanced_scout(win):
    """The scout pose with the Advanced panel of Position X open: the SCOUT
    group with a margin of its own (per axis since 2026-10-09), and the tags
    'scout x3' on both scouted rows. The Scout pass section stays closed, so
    the panel has the room of the axis list."""
    _scout_scan(win)
    b = win.builder
    b.scout_section.set_expanded(False)
    row = next(r for r in b.rows if r.param.id == "pos_x")
    row.margin_auto.setChecked(False)
    row.margin_spin.setValue(2.0)
    b.open_advanced(row)
    b._rebuild_summary()


def _axis_advanced_ramp(win):
    """A FIELD axis flown (2026-10-09): the frequency steps, the magnet's
    field is SWEPT by its module each row (the simulator's field has a ramp
    block, as clMag's does). Its FLY group shows what a ramp needs -- the
    pace in mT/s from the ramp's limits, the row-time alternative, "binned by
    measurement" -- and none of a stage's boxes (no speed knob, no readback)."""
    from PySide6 import QtCore
    win.use_simulator()
    b = win.builder
    b.name_edit.setText("FMR map, field flown")
    b.name_edit.setCursorPosition(0)
    b.add_fixed("pos_x", 30.0)
    b.add_fixed("pos_y", 50.0)
    b.add_axis("rf_freq")
    b.rows[0].start.setValue(700.0); b.rows[0].stop.setValue(1300.0)
    b.rows[0].num.setValue(21)
    b.add_axis("field")
    row = b.rows[1]
    row.start.setValue(10.0); row.stop.setValue(90.0); row.num.setValue(41)
    row.fly.setChecked(True)
    row.speed.setValue(30.0)
    b.zigzag_box.setChecked(True)
    for it in b._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    b.per_pt.setValue(0.05)
    b.open_advanced(row)
    b._rebuild_summary()


def _scout_run(win):
    """A field x frequency map measured only around the resonance line
    (recipes/scout_field_freq.yaml), on the simulator: the live plot shows
    the result, NaN where the scout said 'nothing here'."""
    from scan_core import Recipe
    win.use_simulator()
    b = win.builder
    b.load_recipe(Recipe.load(ROOT / "scan-core" / "recipes" / "scout_field_freq.yaml"))
    b.name_edit.setCursorPosition(0)
    b.per_pt.setValue(0.0)
    b.run_scan(block=True)


def _queue_entries(b):
    """What "Load scan..." with several files selected produces: the example
    recipes as they are on disk, plus one old measurement file naming a lock-in
    that is not connected -- so the picture also shows a refused entry."""
    from scan_core import Recipe, scan_queue
    recipes = ROOT / "scan-core" / "recipes"
    entries = scan_queue.load_definitions([recipes / "field_freq_2d.yaml",
                                           recipes / "xy_raster_field_3d.yaml",
                                           recipes / "field_freq_voltage_3d.yaml"])
    names = ["FMR map, 0-120 mT", "islands vs field", "device voltage cube"]
    for e, n in zip(entries, names):
        e.name = n
        e.source = str(Path(e.source).relative_to(ROOT))   # no user folder in a README
    old = Recipe(name="tc check", axes=[{"type": "linear", "param": "hf2.tc1",
                                         "start": 0.001, "stop": 0.1, "num": 5}],
                 detectors=["hf2.r1"])
    entries.append(scan_queue.QueueEntry(
        "lock-in time constant", old,
        str(Path("data") / "2026-09-20" / "101500_tc_check.nc")))
    return entries


def _queue_dialog(win):
    """The queue dialog as it opens after loading several definitions.
    Returns the DIALOG, which is then photographed instead of the window."""
    from apps.scan_builder import QueueDialog
    win.use_simulator()
    b = win.builder
    dlg = QueueDialog(_queue_entries(b), b.registry, 0.02, win)
    dlg.list.setCurrentRow(0)
    dlg.show()
    win._render_keep = dlg              # keep it alive until the grab
    return dlg


def _queue_running(win):
    """The Measurement tab with a QUEUE running: "Scan 2 of 3", the map of
    the running scan filling in, and the Stop queue button."""
    from scan_core import Recipe, scan_queue
    win.use_simulator()
    b = win.builder
    wait = [{"when": "before_point", "action": "wait_ms", "args": {"ms": 2}}]

    def fmr(name, n_field, n_freq, power):
        return scan_queue.QueueEntry(name, Recipe(
            name=name, fixed={"rf_power": power},
            axes=[{"type": "linear", "param": "field", "start": 0, "stop": 120,
                   "num": n_field},
                  {"type": "linear", "param": "rf_freq", "start": 500, "stop": 2500,
                   "num": n_freq}],
            detectors=["lockin_r"], hooks=wait))
    b.per_pt.setValue(0.004)
    def stop():
        b.stop_queue()
        while b.queue_running() or (b.worker is not None and b.worker.isRunning()):
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)
    from PySide6 import QtWidgets
    win._render_cleanup = stop
    b.run_queue([fmr("FMR map 0 dBm", 12, 20, 0.0),
                 fmr("FMR map 8 dBm", 41, 61, 8.0),
                 fmr("FMR map 14 dBm", 41, 61, 14.0)])


def _watch_server(win, remote: bool = False):
    """The Measurement tab WATCHING a scan server: a server in this process
    (on the simulator, scratch ports) runs a 2-scan queue submitted by "the
    lab PC", and the suite shows it -- header, progress, queue line, live map,
    the server's log. The PC and user names are neutral ("lab-pc", "operator"):
    a screenshot is a tracked file (CLAUDE.md, private names).

    `remote` (phase 2): the suite watches as if from ANOTHER PC
    ("office-pc"), holds control of the server and has added a third scan to
    the running queue -- the queue card's Add / Remove / Up / Down and Copy to
    this PC, and the RUN INFO card that comes back while it may submit."""
    import random
    import socket as _socket
    import tempfile

    # security off for the picture: this PC's keys (and their PC name, which a
    # 'warn' line would print into the log pane) are not what is shown here
    os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="render-nosec-")
    from scan_core import Recipe, build_sim_registry
    from scan_core.scan_server import ScanServer
    from scan_core.scan_server_client import ScanServerClient

    while True:
        cmd = random.randrange(20000, 40000)
        with _socket.socket() as a, _socket.socket() as b_:
            try:
                a.bind(("127.0.0.1", cmd)); b_.bind(("127.0.0.1", cmd + 1))
                break
            except OSError:
                continue
    srv = ScanServer(host="127.0.0.1", cmd_port=cmd, pub_port=cmd + 1,
                     registry=build_sim_registry(), data_dir=NEUTRAL / "data",
                     echo=False, live_every_s=0.3, status_hz=6.0)
    srv.pc = "lab-pc"
    srv.start()
    c = ScanServerClient("127.0.0.1", cmd, cmd + 1)
    c.identity["host"] = "operator@lab-pc"
    c.start()
    wait = [{"when": "before_point", "action": "wait_ms", "args": {"ms": 4}}]

    def fmr(name, power):
        return Recipe(name=name, fixed={"rf_power": power},
                      axes=[{"type": "linear", "param": "field", "start": 0, "stop": 120,
                             "num": 41},
                            {"type": "linear", "param": "rf_freq", "start": 500,
                             "stop": 2500, "num": 61}],
                      detectors=["lockin_r"], hooks=wait)
    c.submit_queue([("FMR map 0 dBm", fmr("FMR map 0 dBm", 0.0)),
                    ("FMR map 8 dBm", fmr("FMR map 8 dBm", 8.0))],
                   attrs={"sample": "YIG islands", "operator": "operator"})
    if remote:
        # "another PC": the watch must not take 127.0.0.1 for this PC
        import apps.scan_server_view as SV
        SV.ServerWatch.is_local = lambda self: False
    w = win.watch_server(f"127.0.0.1:{cmd}:{cmd + 1}")
    if remote:
        # neutral names on the picture (the queue card says who added a scan)
        for cl in (w.client, w._poll):
            cl.identity["host"] = "operator@office-pc"
        import time as _t
        t0 = _t.monotonic()
        while not w.answering and _t.monotonic() - t0 < 10:
            _t.sleep(0.05)
        w.take_control()
        w.client.queue_add(("FMR map 14 dBm", fmr("FMR map 14 dBm", 14.0)),
                           attrs={"sample": "YIG islands", "operator": "office"})

    def cleanup():
        if win.watch is not None:
            win.watch.close()
        c.close()
        srv.stop()                          # aborts the queue, saves, stops
    win._render_cleanup = cleanup


def _fly(run: bool):
    """A FLY scan over the simulated islands: Y stepped, X flown continuously
    at 60 um/s, every other row backwards (zig-zag). The simulator's stage
    really travels and its lock-in stream really lags behind a filter, so the
    image is what the lag correction makes of a genuinely flown scan.
    `run=False` poses the Scan tab only (the ticked fly row and its summary)."""
    def warm_up(win):
        from PySide6 import QtCore
        win.use_simulator()
        b = win.builder
        b.name_edit.setText("fly over the islands")
        b.name_edit.setCursorPosition(0)
        b.registry._state.lockin_tc_s = 0.003
        b.add_fixed("field", 40.0)
        b.add_fixed("rf_freq", 900.0)
        b.add_fixed("rf_power", 8.0)
        b.add_axis("pos_y")
        b.rows[0].start.setValue(-45.0); b.rows[0].stop.setValue(45.0)
        b.rows[0].num.setValue(31)
        b.add_axis("pos_x")
        row = b.rows[1]
        row.start.setValue(-45.0); row.stop.setValue(45.0); row.num.setValue(91)
        row.fly.setChecked(True)
        # 1 um pixels at 60 um/s with the sim's 200 Hz stream: ~3 samples each
        row.speed.setValue(60.0)
        b.zigzag_box.setChecked(True)
        for it in b._det_items():
            it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                             == "lockin_r" else QtCore.Qt.Unchecked)
        b.per_pt.setValue(0.05)
        if run:
            b.run_scan(block=True)
    return warm_up


def _image_scan(run: bool):
    """A camera IMAGE per point (2026-10-10): a toy 12-bit camera added to the
    simulator -- a laser spot on a striped sample that slides under it as X
    moves -- recorded with the lock-in over a 9 x 13 map. `run=False` poses the
    Scan tab (its summary shows the size of the images); `run=True` runs it
    and shows the Measurement tab with the newest frame next to the map."""
    def warm_up(win):
        import numpy as np
        from PySide6 import QtCore
        from scan_core.registry import AxisSpec, Gettable
        from scan_core.storage import Storage
        win.use_simulator()
        b = win.builder
        reg = b.registry
        h, w = 96, 128
        yy, xx = np.mgrid[0:h, 0:w]

        def frame():
            x = reg.get("pos_x").get()
            y = reg.get("pos_y").get()
            stripes = 900 + 500 * (np.sin((xx + 3.0 * x) / 6.0) > 0)
            spot = 2600 * np.exp(-((xx - 64) ** 2 + (yy - 48) ** 2) / (2 * 6.0 ** 2))
            noise = np.random.default_rng(int(1000 + 10 * x + y)).normal(0, 40, (h, w))
            return np.clip(stripes + spot + noise, 0, 4095).astype(np.uint16)
        axes = [AxisSpec("camera.image_y", "image y", "px", length=h,
                         values_fn=lambda: np.arange(h, dtype=float)),
                AxisSpec("camera.image_x", "image x", "px", length=w,
                         values_fn=lambda: np.arange(w, dtype=float))]
        reg.add(Gettable("camera.image", "Camera image", "counts", frame, axes=axes,
                         dtype="int", storage=Storage("int", bits=12)))
        b.set_registry(reg)
        b.name_edit.setText("spot images")
        b.name_edit.setCursorPosition(0)
        b.add_fixed("field", 40.0)
        b.add_axis("pos_y")
        b.rows[0].start.setValue(-20.0); b.rows[0].stop.setValue(20.0)
        b.rows[0].num.setValue(9)
        b.add_axis("pos_x")
        b.rows[1].start.setValue(-30.0); b.rows[1].stop.setValue(30.0)
        b.rows[1].num.setValue(13)
        for it in b._det_items():
            it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                             in ("lockin_r", "camera.image") else QtCore.Qt.Unchecked)
        b.per_pt.setValue(0.0)
        if run:
            b.run_scan(block=True)
            b.view.apply_view_state({"detector": "lockin_r"})
    return warm_up


def _scan_3d_then_show_data(win):
    """Frequency x Y x X over the simulated patterned sample.

    A cube is what the Data tab is FOR: the third dimension gets a control of
    its own instead of being silently sliced at index 0. This scan earns that
    control -- every island resonates at its own frequency, so the slider walks
    through a different picture of the same array at each step.
    """
    win.use_simulator()
    b = win.builder
    b.name_edit.setText("fmr islands")
    # Context the axes do not drive: a working point where the array is alive.
    b.registry.get("field").set(40.0)
    b.registry.get("rf_power").set(8.0)
    b.add_axis("rf_freq")
    b.rows[0].start.setValue(400.0); b.rows[0].stop.setValue(1500.0)
    b.rows[0].num.setValue(12)
    b.add_axis("pos_y")
    b.rows[1].start.setValue(-45.0); b.rows[1].stop.setValue(45.0)
    b.rows[1].num.setValue(61)
    b.add_axis("pos_x")
    b.rows[2].start.setValue(-45.0); b.rows[2].stop.setValue(45.0)
    b.rows[2].num.setValue(61)
    b.per_pt.setValue(0.0)
    b.run_scan(block=True)
    win._show_last_run()
    rows = win.data_view.map.controls.rows
    if rows:
        # One frequency, not the average. 900 MHz is the richest slice: the
        # film between the islands is near its own resonance, so the pattern
        # reads as dark elements on a lit background, and the one island whose
        # resonance sits at 890 MHz is lit right through. Move the slider and
        # a different element lights up -- which is the point of the control.
        rows[0].slider.setValue(5)


def _catalogue_demo(win):
    """The Catalogue tab over a small, believable data folder: a week of runs on
    two samples by two people, with conditions and instrument snapshots, one
    unreadable file -- and a search typed in, so the picture shows what the tab
    is FOR (narrowing a folder of files to the few you want).

    The files are written by the real engine on the simulator into their own
    scratch folder (never the suite's data folder), then indexed synchronously.
    """
    import json
    import shutil
    from scan_core import Recipe, build_sim_registry, run

    folder = NEUTRAL / "catalogue-demo"
    shutil.rmtree(folder, ignore_errors=True)
    runs = [  # (day, time, name, sample, structure, operator, tags, T, field, axes)
        ("2026-09-28", "101500", "fmr field sweep", "B7", "disc array 2 um", "alice",
         "fmr", 300.0, 40.0, [("field", 0, 120, 41)]),
        ("2026-09-29", "143000", "fmr islands", "B7", "disc array 2 um", "alice",
         "fmr, map", 300.0, 40.0, [("rf_freq", 400, 1500, 12), ("pos_y", -45, 45, 31),
                                   ("pos_x", -45, 45, 31)]),
        ("2026-09-30", "091000", "cold fmr", "B7", "disc array 2 um", "bob",
         "fmr, cryo", 5.0, 50.0, [("rf_freq", 500, 2500, 81)]),
        ("2026-10-01", "112000", "cold fmr vs field", "B7", "disc array 2 um", "bob",
         "fmr, cryo", 5.0, 50.0, [("field", 0, 120, 41), ("rf_freq", 500, 2500, 81)]),
        ("2026-10-02", "160500", "moke loop", "Y12", "YIG film 100 nm", "alice",
         "moke", 300.0, 0.0, [("field", -80, 80, 161)]),
        ("2026-10-03", "100000", "kerr map", "Y12", "YIG film 100 nm", "alice",
         "moke, map", 300.0, 20.0, [("pos_y", -30, 30, 41), ("pos_x", -30, 30, 41)]),
    ]
    for day, hms, name, sample, structure, op, tags, temp, field, axes in runs:
        reg = build_sim_registry()
        rec = Recipe(name=name, fixed={"rf_power": 8.0},
                     detectors=["lockin_r", "lockin_x"],
                     axes=[{"type": "linear", "param": p, "start": a, "stop": b,
                            "num": n} for p, a, b, n in axes])
        ds = run(rec, reg, created_iso=f"{day}T{hms[:2]}:{hms[2:4]}:{hms[4:]}")
        ds.attrs.update(sample=sample, structure=structure, operator=op,
                        project="magnonics", tags=tags, series="S1",
                        snapshot_modules="ppms,clMag",
                        snapshot_ppms=json.dumps({"status": {"temperature": temp}}),
                        snapshot_clMag=json.dumps({"status": {"field": field}}))
        path = folder / day / f"{hms}_{name.replace(' ', '_')}.nc"
        path.parent.mkdir(parents=True, exist_ok=True)
        ds.to_netcdf(path)
    (folder / "2026-10-03" / "093000_interrupted.nc").write_bytes(b"truncated")
    cat = win.catalogue
    cat.set_data_dir(folder)
    cat.rescan(); cat.wait_scan()
    cat.sample_edit.setText("B7")
    cat.refresh()
    if cat.table.topLevelItemCount():
        cat.table.setCurrentItem(cat.table.topLevelItem(0))


def _dynacool_controls(win):
    """The DynaCool VNA-FMR setup (ppms + vna) on the Control tab: the
    cryostat's own knobs, the "reached" flags a scan waits on, and the field
    the VNA hears -- with a field ramp under way, so the chart shows it moving.

    Needs the two services running (SUITE_RENDER_MODULES=ppms,vna); the ramp is
    sent over the wire like any client would, before the settle time starts."""
    import zmq
    from PySide6 import QtWidgets, QtCore

    wanted = {"ppms.field", "ppms.measured_field", "ppms.field_status", "ppms.field_stable",
              "ppms.temperature", "ppms.measured_temperature", "ppms.temperature_stable",
              "ppms.chamber", "vna.field", "vna.reference_field"}
    panel = win.control
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value():
        node = it.value()
        if node.data(0, QtCore.Qt.UserRole) in wanted:
            node.setCheckState(0, QtCore.Qt.Checked)
        it += 1
    panel._rebuild_panel()
    req = zmq.Context.instance().socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 3000)
    req.setsockopt(zmq.LINGER, 0)
    req.connect("tcp://127.0.0.1:5579")
    for msg in ({"cmd": "set_field", "field_mT": 100.0},
                {"cmd": "set_temperature", "temperature_K": 300.0}):
        req.send_json(msg)
        req.recv_json()
    req.close(0)


def _viewer(tab: str):
    """The data viewer on a folder of SIMULATED measurements (a temporary one).

    Real scans would be nicer to look at but do not belong in a repo, and a
    screenshot must not depend on what happens to be in someone's data folder.
    The files are named like autosaves (<date>/<HHMMSS>_<name>.nc) so the file
    list shows what it shows in the lab.
    """
    def make(theme):
        import tempfile
        sys.path.insert(0, str(ROOT / "scan-core"))
        from scan_core import Recipe, build_sim_registry, run
        from apps import viewer

        # A fresh, NEUTRAL folder (see NEUTRAL): its path is on screen.
        import shutil
        shutil.rmtree(NEUTRAL / "viewer-demo", ignore_errors=True)
        data = NEUTRAL / "viewer-demo" / "2026-09-16"
        data.mkdir(parents=True)
        recipes = ROOT / "scan-core" / "recipes"
        files = {}
        for stem, recipe in (("101512_fmr_map", "field_freq_2d.yaml"),
                             ("113040_voltage_cube", "field_freq_voltage_3d.yaml"),
                             ("140207_xy_image", "xy_raster_field_3d.yaml")):
            ds = run(Recipe.load(recipes / recipe), build_sim_registry())
            files[stem] = data / f"{stem}.nc"
            ds.to_netcdf(files[stem], engine="h5netcdf")

        def show():
            return viewer.main(["--theme", theme, "--folder", str(data.parent)])

        def warm_up(win):
            v = win.viewer
            m = v.map
            if tab == "map":
                # The spatial cube: an FMR image of the patterned sample at each
                # field. It says more about the viewer than a field-frequency
                # map does -- you are looking at the sample, and the cursor is
                # on one element.
                v.load_file(files["140207_xy_image"])
                m.controls.x_combo.setCurrentText("pos_x")
                m.controls.y_combo.setCurrentText("pos_y")
                m.controls.rows[0].slider.setValue(3)      # field = 45 mT
                m.refresh()
                # (index x, index y) over -45..45 um in 61 points: the bar at
                # (-30, -6), which is the element resonating at this field.
                m._place_cursor(10, 26)
                m._cut("row")                              # R(x) across the array
                v.browser.tree.setCurrentItem(v.browser.tree.topLevelItem(0))
                v.tabs.setCurrentWidget(m)
                return

            v.load_file(files["113040_voltage_cube"])
            m.controls.x_combo.setCurrentText("field")
            m.controls.y_combo.setCurrentText("rf_freq")
            m.controls.rows[0].slider.setValue(3)          # device_v = +2 V
            m.refresh()
            m._place_cursor(18, 30)
            m._cut("column")                               # R(freq) at one field
            lines = v.lines
            lines.controls.x_combo.setCurrentText("rf_freq")
            for r in lines.controls.rows:
                if r.dim == "field":
                    r.slider.setValue(18)
            lines.along_combo.setCurrentText("device_v")
            for i in range(lines.values.count()):
                lines.values.item(i).setSelected(True)
            lines.add_selected()
            lines.norm_combo.setCurrentIndex(1)            # peak-normalised
            lines.offset_edit.setText("0.5"); lines.redraw()
            v.browser.tree.setCurrentItem(v.browser.tree.topLevelItem(1))
            v.tabs.setCurrentWidget(lines)

        return show, warm_up, 2.5
    return make


def _no_security_setup():
    """A published picture must not show THIS PC's keys or keyring: point the
    security folder at an empty one (the badge then says "Security: off")."""
    import tempfile
    os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="render-nosec-")


def _neutral_local_settings():
    """This PC's suite_local.json holds its REMOTE services (a lab PC's
    address, its name) and its own setup name: never in a published picture.
    The render sees the modules of the folder and nothing of this PC's own
    settings (2026-10-06: a remote scan server card showed a lab IP)."""
    from suite_common import modules as _m
    real = _m.load_local
    _m.load_local = lambda root=None: {**real(root), "remote": [], "settings": {}}
    # ... and the render WRITES nothing: a setting saved meanwhile would write
    # the stripped copy back over this PC's real file and lose its remotes
    _m.save_local = lambda data, root=None: None


def _mission_control(theme):
    _no_security_setup()
    _neutral_local_settings()
    sys.path.insert(0, str(ROOT / "mission-control"))
    import mission_control

    def show():
        return mission_control.main(["--theme", theme])

    def warm_up(win):
        # The log starts with this PC's paths (root, uv): not for a published
        # picture. Show what a fresh install would say instead.
        box = getattr(win, "logbox", None)
        if box is not None:
            box.clear()
            win.log(r"root = C:\AaltoFlow")
            win.log("12 modules found, ready")

    return show, warm_up, 3.0


def _mission_control_instruments(theme):
    """Instruments on this PC, on a made-up lab: the scan is replaced by a
    fixed list (no PC renders this with a GPIB card attached), with invented
    addresses and no serial numbers -- a published picture names no real
    instrument."""
    _no_security_setup()
    sys.path.insert(0, str(ROOT / "mission-control"))
    import mission_control
    from suite_common.instruments import Found

    rows = [
        Found("GPIB0::6::INSTR", "gpib", held_by="kepco",
              detail="held by a running service: not opened"),
        Found("GPIB0::8::INSTR", "gpib", identity="Stanford_Research_Systems,SR830,s/n00000,ver1.07",
              asked=True),
        Found("GPIB0::28::INSTR", "gpib", identity="Rohde&Schwarz,SMB100A,1406.6000k03/000000,3.1.19",
              asked=True),
        Found("GPIB0::9::INSTR", "gpib", error="no answer to *IDN? (timeout)", asked=True),
        Found("USB0::0x1313::0x8078::P0000000::INSTR", "usb",
              identity="Thorlabs,PM100D,P0000000,2.8.0", asked=True),
        Found("TCPIP0::192.168.1.20::inst0::INSTR", "tcpip",
              identity="Keysight Technologies,N5222A,MY00000000,A.13.95", asked=True),
        Found("COM3", "serial", identity="USB Serial Port (COM3)", detail="FTDI, USB 0403:6001",
              held_by="superk", aliases=["ASRL3::INSTR"]),
        Found("COM5", "serial", identity="Silicon Labs CP210x USB to UART Bridge (COM5)",
              detail="Silicon Labs, USB 10C4:EA60", aliases=["ASRL5::INSTR"]),
        Found("COM7", "serial", identity="USB Serial Device (COM7)",
              detail="Microsoft, USB 0483:5740", aliases=["ASRL7::INSTR"]),
    ]
    # the list-only sources: module probes and the USB device list (made-up
    # serials; an IDS camera's real Windows name would carry its serial)
    from suite_common import instruments as I
    from suite_common.usb_devices import UsbDevice
    probes = []
    for key, devs in (
            ("kim", [{"address": "97000000", "identity": "Thorlabs KIM101",
                      "detail": "held by the running kim service (a controller is not "
                                "listed while it is open)"}]),
            ("camera", [{"address": "4100000000", "identity": "IDS U3-0000XCP-M",
                         "detail": "IDS peak, serial 4100000000",
                         "lock": "CAMERA::4100000000"}]),
            ("usb6001", [{"address": "Dev1", "identity": "NI USB-6001",
                          "detail": "DAQmx name Dev1, serial 00000000"}]),
            ("pm16", [{"address": "USB0::0x1313::0x807B::000000000::INSTR",
                       "identity": "Thorlabs PM160", "detail": "S/N 000000000, TLPMX"}])):
        r, _ = I.parse_probe(json.dumps({"devices": devs}), key)
        probes += r
    probes[0].held_by = "kim"
    usb = I.usb_rows([UsbDevice(0x0403, 0xFAF0, "APT USB Device", "USB", "97000000"),
                      UsbDevice(0x1313, 0x807B, "PM160", "ThorlabsUSBDevice", "000000000"),
                      UsbDevice(0x0403, 0x6010, "USB Composite Device", "USB", "SH000000"),
                      UsbDevice(0x0403, 0x6001, "USB Serial Port (COM4)", "Ports", "A0000000"),
                      UsbDevice(0x413C, 0x301A, "USB Input Device", "HIDClass", "")], held={})
    vendor = I.merge(probes, usb)
    mission_control.finder.scan = lambda ask_visa=True: (list(rows), [])
    mission_control.finder.scan_vendor = lambda *a, **k: (
        list(vendor), ["hf2: zhinst-core is not installed: install LabOne, then add "
                       "zhinst-core (the same version) to hf2-control and uv sync"])

    def show():
        return mission_control.main(["--theme", theme])

    def warm_up(win):
        box = getattr(win, "logbox", None)
        if box is not None:
            box.clear()
        dlg = win.show_instruments()
        from PySide6 import QtWidgets
        for _ in range(20):
            QtWidgets.QApplication.processEvents()
        # the IDS camera, found by the camera module's probe: "Use for module..." lit
        row = next((i for i, f in enumerate(dlg.shown) if f.address == "4100000000"), 0)
        dlg.table.selectRow(row)
        return dlg

    return show, warm_up, 1.5


def _mission_control_security(tab: int):
    """The Security window on a made-up lab: lab-pc-1 (this PC, may run
    scans), office-1, a key file this PC cannot read (laptop-2), a retired
    old-laptop, policy warn on all modules. Everything lives in a temporary
    folder; the PC name is made up, so no real name is in the picture."""
    def make(theme):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="render-sec-"))
        os.environ["AALTOFLOW_SECURITY_DIR"] = str(tmp / "me")
        sys.path.insert(0, str(ROOT / "mission-control"))
        import mission_control
        from suite_common import keyadmin, secure
        secure.this_pc_name = lambda: "lab-pc-1"
        kr = tmp / "keyring"
        keyadmin.init_keyring(kr, "warn", ["*"])
        keyadmin.make_key(machine=True, pc="lab-pc-1", addresses=["10.0.0.5"])
        for pc, machine, addr in (("office-1", False, "10.0.0.17"),
                                  ("analysis-pc", False, ""), ("old-laptop", False, "")):
            pub, _ = secure.new_keypair()
            meta = {"pc": pc, "machine": "yes" if machine else "no"}
            if addr:
                meta["addresses"] = addr
            secure.write_cert(kr / f"{pc}.key", pub, meta=meta)
        keyadmin.retire("old-laptop")
        pub, _ = secure.new_keypair()
        secure.write_cert(kr / "laptop-2.key", pub, meta={"pc": "laptop-2"})
        real = secure.read_cert

        def read(path):                         # as on the lab share (2026-09-30)
            if Path(path).name == "laptop-2.key":
                raise PermissionError(13, "Access is denied", str(path))
            return real(path)
        secure.read_cert = read

        def show():
            return mission_control.main(["--theme", theme])

        def warm_up(win):
            box = getattr(win, "logbox", None)
            if box is not None:
                box.clear()
            dlg = win.show_security()
            dlg.tabs.setCurrentIndex(tab)
            if tab == 1:
                dlg.select_pc("office-1")
            if tab == 2:
                dlg._show_stale([{"module": "pm16", "mode": "off"},
                                 {"module": "kim", "mode": "off"}])
            dlg.say("lab policy is now warn (all modules)", "ok")
            return dlg

        return show, warm_up, 1.0
    return make


def _pm16(theme):
    from pm16.config import Config
    from pm16.sim_system import build_sim_system
    from pm16.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    meter, _ = build_sim_system(cfg)

    def warm_up(win):
        # the sim laser's wavelength, and one latched sample for the sidebar
        win.ctrl.set_wavelength(800.0)
        win.ctrl.acquire()

    return lambda: gui.run_app(meter, cfg), warm_up, 4.0


def _ppms(theme):
    from ppms.config import Config
    from ppms.sim_system import build_sim_system
    from ppms.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    cfg.hardware.poll_s = 0.1
    # a cold sample in a field, as it would sit during an FMR run
    cryo, _ = build_sim_system(cfg, field_mT=0.0, temperature_K=10.0, seed=3)

    def warm_up(win):
        # a ~5 s ramp to 100 mT (fills the field chart, reached before the
        # grab) and a small temperature step that is still settling
        win.ctrl.set_field(100.0)
        win.ctrl.set_temperature(12.0)

    return lambda: gui.run_app(cryo, cfg), warm_up, 9.0


def _vna(theme):
    from vna.config import Config
    from vna.sim_system import build_sim_system
    from vna.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    # manual field: a render must not depend on a magnet service being up
    cfg.field.source = "manual"
    # Start at 0 mT, where the line is below the band, so the reference taken
    # in the warm-up really is "the cables without the sample". A 1.2 GHz span
    # around the 45 mT line keeps the ~15 MHz resonance wider than a pixel.
    cfg.field.manual_mT, cfg.field.manual_angle_deg = 0.0, 45.0
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 2.2e9, 3.4e9, 1201
    # two fly-scan stream points, so the "Fly points" box shows what it is for
    cfg.stream.points_Hz = "2.6e9, 2.8e9"
    vna, _ = build_sim_system(cfg, seed=5)

    def warm_up(win):
        # The VNA-FMR routine in miniature: reference at 0 mT, then the field,
        # then one latched acquisition for the sidebar. The plot shows u, which
        # only exists relative to that reference; continuous sweeps fill it.
        n = win.ctrl.take_reference()
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            st = win.ctrl.status()
            if st.acq_id == n and not st.acquiring:
                break
            time.sleep(0.05)
        win.ctrl.set_manual_field(45.0, 45.0)
        win.view_combo.setCurrentIndex(2)            # u (real + imag)
        win.ctrl.acquire()

    return lambda: gui.run_app(vna, cfg), warm_up, 3.0


def _mag2d(theme):
    from PySide6 import QtCore
    from mag2d.config import Config
    from mag2d.sim_system import build_sim_system
    from mag2d.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg, seed=5)
    # MainWindow does not start the brain (gui.main does), so do it here.
    ctrl.start()

    def warm_up(win):
        # Two steps, so the strip chart shows the loop working: 80 mT along +X,
        # then 150 mT at 45 deg -- the reference field of the VNA-FMR recipe.
        # 150 mT @ 45 deg takes ~3.5 s at the 2 V/s slew; the grab waits 8 s.
        win.ctrl.set_field(80.0, 0.0)
        win.field_spin.setValue(150.0)
        win.angle_spin.setValue(45.0)
        QtCore.QTimer.singleShot(2500, win._go_polar)

    return lambda: gui.run_app(ctrl, cfg), warm_up, 8.0


def _mag2dcal(theme):
    from PySide6 import QtCore
    from mag2dcal.calibration import AxisCalibration, Calibration
    from mag2dcal.config import Config
    from mag2dcal.sim_system import build_sim_system
    from mag2dcal.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    # Do NOT pick up whatever happens to be in the project's Calibrations
    # folder: the picture has to be the same every time it is rendered.
    cfg.calibration.load_newest_on_start = False
    ctrl, sim = build_sim_system(cfg, seed=5)
    ctrl.start()

    def _leg(gain, h, sign):
        """A plausible measured leg: B = gain*V + sign*h, 21 points over +-5 V."""
        return [(-5.0 + 0.5 * i, gain * (-5.0 + 0.5 * i) + sign * h) for i in range(21)]

    # A calibration is installed so the card, the limits and the viewer all show
    # something real -- running a live sweep would take a minute of wall clock.
    ctrl.set_calibration(Calibration(
        axes=[AxisCalibration(up=_leg(20.0, 0.4, +1), down=_leg(20.0, 0.4, -1)),
              AxisCalibration(up=_leg(19.4, 0.4, +1), down=_leg(19.4, 0.4, -1))],
        note="measured on the simulator"))

    def warm_up(win):
        # Two steps, so the strip chart shows the seek working: 40 mT along +X,
        # then 70 mT at 45 deg -- and the second one is still settling when the
        # grab happens, so both the moving and the frozen states are on show.
        win.ctrl.set_field(40.0, 0.0)
        win.field_spin.setValue(70.0)
        win.angle_spin.setValue(45.0)
        QtCore.QTimer.singleShot(4000, win._go_polar)

    return lambda: gui.run_app(ctrl, cfg), warm_up, 10.0


def _control(as_viewer: bool):
    """The smb window connected to a service (``--connect``), with the CONTROL
    bar at the top (control.py / apps/control_bar.py): once as the GUI that
    holds control, once as a VIEWER on another PC.

    Everything runs in this one process: a simulated SmbService on scratch
    ports, the GUI's own client, and the other clients the bar reports (a
    second GUI, scan-core as a machine that is "also driving"). Their "user@PC"
    names are invented on purpose -- a published picture must not show the
    account or PC it was rendered on, and the real identity of this process
    would be exactly that.
    """
    def make(theme):
        from smb.config import Config
        from smb.sim_system import build_sim_system
        from smb.apps import gui
        from smb.net.client import SmbClient
        from smb.net.service import SmbService

        cmd, pub = 18990, 18991          # scratch ports, not the module's own
        gen, _ = build_sim_system(Config())
        svc = SmbService(gen, host="127.0.0.1", cmd_port=cmd, pub_port=pub, status_hz=10)
        svc.start()
        others = []

        def client(kind, name, host):
            c = SmbClient(host="127.0.0.1", cmd_port=cmd, pub_port=pub,
                          kind=kind, name=name)
            c.identity["host"] = host
            c.start()                    # heartbeats: the bar lists it
            return c

        # the "other PC": a GUI that holds control (viewer shot) or only
        # watches (holder shot)
        other = client("gui", "smb GUI", "student@lab-pc-2")
        others.append(other)
        if as_viewer:
            other.take_control()
        # scan-core, a machine client, changed something a moment ago: every
        # bar then says "also driving: scan-core"
        scan = client("machine", "scan-core", "operator@lab-pc-1")
        others.append(scan)
        scan.set_frequency(2.45e9)
        scan.set_power(-3.0)
        scan.set_rf(True)

        me = SmbClient(host="127.0.0.1", cmd_port=cmd, pub_port=pub,
                       kind="gui", name="smb GUI")
        me.identity["host"] = "operator@lab-pc-1"
        me.start()
        me.cfg.ui.theme = theme

        def warm_up(win):
            def cleanup():
                for c in others + [me]:
                    try:
                        c.shutdown()
                    except Exception:
                        pass
                svc.stop()
            win._render_cleanup = cleanup

        return lambda: gui.run_app(me, me.cfg, remote=True), warm_up, 3.0
    return make


def _afg(theme):
    from afg.config import Config
    from afg.sim_system import build_sim_system
    from afg.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    gen, _ = build_sim_system(cfg)

    def warm_up(win):
        # The bench use: CH1 a 30 Hz sine (the drive), CH2 a square locked to
        # it a quarter period later (the scope's trigger), both on.
        win.ctrl.set_offset("ch2", 0.0)
        win.ctrl.set_amplitude("ch2", 1.0)
        win.ctrl.set_follow(True, 90.0)
        win.ctrl.set_output("ch2", True)

    return lambda: gui.run_app(gen, cfg), warm_up, 2.0


def _scope(theme, tab: int = 0):
    import os
    import tempfile
    # the window's remembered tab / splitter must not come from this PC
    os.environ["AALTOFLOW_GUI_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "gui.ini")
    from scope.config import Config
    from scope.sim_system import build_sim_system
    from scope.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    scope, _ = build_sim_system(cfg, seed=3)

    def warm_up(win):
        # CH1 through a current probe (0.1 V/A -> 10 A/V), CH2 in volts; a
        # light low-pass
        win.ctrl.set_physical("ch1", scale=10.0, unit="A", label="Current")
        win.ctrl.set_physical("ch2", label="Signal")
        win.ctrl.set_filter(lowpass_Hz=2000.0)
        win.ctrl.set_averages(16)
        win.tabs.setCurrentIndex(tab)

    return lambda: gui.run_app(scope, cfg), warm_up, 3.5


def _scope_ad(theme):
    """The scope module on a (simulated) Analog Discovery: the Generator tab
    with W1 a 1 kHz sine and W2 a square following it, a quarter period later
    (looped back to CH1/CH2), and V+ on at 3.3 V."""
    import os
    import tempfile
    os.environ["AALTOFLOW_GUI_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "gui.ini")
    from scope.config import Config
    from scope.sim_system import build_sim_system
    from scope.apps import gui

    cfg = Config()
    cfg.ui.theme = theme
    cfg.sim.model = "ad"
    scope, _ = build_sim_system(cfg, seed=3)

    def warm_up(win):
        g = win.ctrl.gen
        g.set_frequency("w1", 1000.0); g.set_amplitude("w1", 2.0); g.set_output("w1", True)
        g.set_waveform("w2", "square"); g.set_amplitude("w2", 1.0)
        g.set_follow(True, 90.0, True); g.set_output("w2", True)
        win.ctrl.set_supply("vplus", on=True, volts=3.3)
        win.ctrl.set_tdiv(2e-4)
        win.tabs.setCurrentIndex(win.tabs.count() - 1)

    return lambda: gui.run_app(scope, cfg), warm_up, 3.5


TARGETS = {
    "control-holder": _control(as_viewer=False),
    "control-viewer": _control(as_viewer=True),
    "pm16": _pm16,
    "ppms": _ppms,
    "mag2d": _mag2d,
    "mag2dcal": _mag2dcal,
    "vna": _vna,
    "suite-control": _suite("Control", _tick_some_controls, settle=4.0, follow=True),
    # SUITE_RENDER_MODULES=ppms,vna with both services up
    "suite-control-dynacool": _suite("Control", _dynacool_controls, settle=13.0, follow=True),
    "suite-scan": _suite("Scan", _stack_two_axes, settle=3.0),
    "suite-measurement": _suite("Measurement", _scan_then_show_run, settle=3.5),
    "suite-data": _suite("Data", _scan_3d_then_show_data, settle=3.5),
    "suite-settings": _suite("Settings", settle=2.0),
    "suite-catalogue": _suite("Catalogue", _catalogue_demo, settle=1.5),
    "suite-navigator": _suite("Navigator", _navigator_demo, settle=2.0),
    "suite-queue": _suite("Measurement", _queue_running, settle=2.5),
    "suite-watch": _suite("Measurement", _watch_server, settle=4.0),
    "suite-watch-remote": _suite("Measurement", lambda w: _watch_server(w, remote=True),
                                 settle=4.0),
    "suite-queue-dialog": _suite("Measurement", _queue_dialog, settle=1.0),
    "suite-fly-scan": _suite("Scan", _fly(run=False), settle=2.0),
    "suite-repeat-scan": _suite("Scan", _stack_with_repeats, settle=2.0),
    "suite-fly": _suite("Measurement", _fly(run=True), settle=3.0),
    "suite-scout-scan": _suite("Scan", _scout_scan, settle=2.0),
    "suite-scout": _suite("Measurement", _scout_run, settle=3.0),
    "suite-axis-advanced": _suite("Scan", _axis_advanced, settle=2.0),
    "suite-axis-advanced-scout": _suite("Scan", _axis_advanced_scout, settle=2.0),
    "suite-axis-advanced-ramp": _suite("Scan", _axis_advanced_ramp, settle=2.0),
    "suite-image-scan": _suite("Scan", _image_scan(run=False), settle=2.0),
    "suite-image": _suite("Measurement", _image_scan(run=True), settle=3.0),
    "camera-settings": lambda theme: _camera(theme, tab="Camera settings"),
    "suite-routine-args": _suite("Scan", _routine_args, settle=2.0),
    "viewer-map": _viewer("map"),
    "viewer-1d": _viewer("1d"),
    "clMag": _clMag,
    "smb": _smb,
    "afg": _afg,
    "scope": _scope,
    "scope-xy": lambda theme: _scope(theme, tab=1),
    "scope-ad": _scope_ad,
    "stage": _stage,
    "piezo": _piezo,
    "camera": _camera,
    "kim": _kim,
    "hf2": _hf2_tab(0),
    "hf2-ch2": _hf2_tab(1),
    "hf2-aux": _hf2_tab(2),
    "hf2-instrument": _hf2_tab(3),
    "scan-core": _scan_core,
    "mission-control": _mission_control,
    "mission-control-instruments": _mission_control_instruments,
    "mission-control-security-pc": _mission_control_security(0),
    "mission-control-security-pcs": _mission_control_security(1),
    "mission-control-security-policy": _mission_control_security(2),
}


def _generic(name: str):
    """Fallback for a module with no hand-made target above.

    Modules built from the template (tools/new_module.py) have
    `<package>.apps.gui.main(theme=...)`, which builds the simulator and shows
    the window. No warm-up, so the panel shows the simulator's start state --
    add a real target here once the module is worth a better picture.
    """
    import importlib
    import inspect

    def make(theme):
        gui = importlib.import_module(f"{name}.apps.gui")
        main = getattr(gui, "main", None)
        if main is None or "theme" not in inspect.signature(main).parameters:
            raise SystemExit(f"{name}: no render target, and {name}.apps.gui has no "
                             f"main(theme=...) to fall back on -- add one to TARGETS")
        return (lambda: main(theme=theme)), None, 3.0
    return make


# ------------------------------- the render ---------------------------------

def render(name: str, out: Path, theme: str = "dark",
           size: tuple[int, int] | None = None) -> Path:
    if name not in TARGETS:
        TARGETS[name] = _generic(name)
    size = size or SIZES.get(name, (1280, 860))

    from PySide6 import QtGui, QtWidgets

    # Create the QApplication HERE, before the target builds anything, so we can
    # pin the application font. run_app() does `QApplication.instance() or
    # QApplication([])`, so it adopts this one.
    #
    # Why bother: clMag and smb set a base `font-family` in their stylesheet, but
    # stage, piezo, camera and kim do not -- they inherit the application font.
    # On a real Windows desktop that is Segoe UI and everything looks right, but
    # under the offscreen platform's basic font database it resolves to a face
    # whose glyphs come out scrambled ("POSITION" renders as "P-SITI---").
    # Pinning the font makes the render match the desktop instead of documenting
    # an artefact of the screenshot tool.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setFont(QtGui.QFont("Segoe UI", 9))

    show, warm_up, settle_s = TARGETS[name](theme)
    captured: dict = {}

    def pump(app, seconds: float, step: float = 0.02):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(step)

    def patched_exec(self, *args, **kwargs):
        """Stand in for the blocking event loop: pump, pose, grab, return."""
        # Pin the font AGAIN, now that the widgets exist. Every run_app() calls
        # app.setStyle("Fusion") after we created the application, and that
        # re-polish discards the font pinned up front -- which is why some
        # modules came out fine and others came out scrambled.
        _pin_font(self)

        pump(self, 0.4)                       # let start() and the first poll land

        win = _top_window(self)
        if win is None:
            raise RuntimeError(f"{name}: no top-level window was created")

        main_win = win
        if warm_up is not None:
            try:
                # A warm-up may hand back another widget to photograph -- a
                # dialog, which is its own top-level window.
                other = warm_up(win)
                if isinstance(other, QtWidgets.QWidget):
                    win = other
            except Exception as exc:          # a bad pose must not lose the shot
                print(f"  warn: warm-up failed ({exc}); rendering idle panel")

        if size:
            win.resize(*size)
        pump(self, settle_s)                  # timers fire, sim converges, paints run

        out.parent.mkdir(parents=True, exist_ok=True)
        shot = win.grab()
        crop_h = CROP_HEIGHT.get(name)
        if crop_h and shot.height() > crop_h:
            shot = shot.copy(0, 0, shot.width(), crop_h)
        ok = shot.save(str(out))
        # A pose that leaves work running (a scan queue: worker THREADS) says how
        # to stop it; exiting with a QThread still running aborts the process.
        cleanup = getattr(main_win, "_render_cleanup", None)
        if cleanup is not None:
            cleanup()
        captured["ok"] = ok
        captured["size"] = (shot.width(), shot.height())
        return 0

    QtWidgets.QApplication.exec = patched_exec
    show()

    if not captured.get("ok"):
        raise SystemExit(f"{name}: grab().save() failed for {out}")
    w, h = captured["size"]
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out                      # --out may point outside the repo
    print(f"  {name:16s} -> {shown}  ({w}x{h}, {out.stat().st_size // 1024} KB)")
    return out


def _pin_font(app):
    """Force one known-good UI font across the whole application.

    Widgets that inherit the application font pick this up directly; widgets
    whose font was set explicitly during construction (big readouts, monospace
    logs) keep their own family but are re-based on this one, so they resolve to
    a real face instead of the offscreen platform's broken default.
    """
    from PySide6 import QtGui

    base = QtGui.QFont("Segoe UI", 9)
    app.setFont(base)
    for w in app.allWidgets():
        f = w.font()
        if f.family() in ("", base.family()):
            w.setFont(base)
        else:                       # keep size/weight/family intent, fix the face
            w.setFont(QtGui.QFont(f))
    app.setStyleSheet(app.styleSheet())     # re-polish so the change propagates


def _top_window(app):
    """The main window, preferring a real QMainWindow over stray dialogs."""
    tops = [w for w in app.topLevelWidgets() if w.isVisible()] or app.topLevelWidgets()
    from PySide6 import QtWidgets
    for w in tops:
        if isinstance(w, QtWidgets.QMainWindow):
            return w
    return tops[0] if tops else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # not restricted to TARGETS: any module key falls back to _generic()
    ap.add_argument("target", help="module key, or one of: " + ", ".join(sorted(TARGETS)))
    ap.add_argument("--theme", default="dark", choices=("dark", "light"))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    args = ap.parse_args()

    out = args.out or PANELS / f"{args.target}.png"
    size = None
    if args.width and args.height:
        size = (args.width, args.height)
    render(args.target, out, theme=args.theme, size=size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
