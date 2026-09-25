"""Render a module's front panel to a PNG, offscreen, with no display.

The suite's GUIs are the documentation nobody reads until they see it, so every
module README shows its panel. Refreshing those by hand (open the GUI, arrange
it, alt-print-screen, crop) is tedious enough that the images go stale, which is
worse than having none. This does it reproducibly instead.

Run it from inside the project whose panel you want, because it imports that
project's package from that project's environment:

    cd clMag-control
    uv run --extra gui python ../tools/render_panels.py clMag

    cd ../mission-control
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
    "stage": (1280, 800),
    "piezo": (1240, 760),
    "camera": (1400, 940),
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
    "suite-control": (1500, 950),
    "suite-scan": (1500, 950),
    "suite-measurement": (1500, 950),
    "suite-data": (1500, 950),
    "suite-settings": (1500, 860),
    "suite-queue": (1500, 950),
    "suite-queue-dialog": (760, 430),
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


def _camera(theme):
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
        fn = getattr(getattr(win, "ctrl", None), "set_tracking", None)
        if fn:
            fn(True)

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
    # ...and the ROUTINES around it: a reference at a far-off field before the
    # scan (the field goes back to its condition before the first point), and
    # the field to 0 afterwards.
    if getattr(b, "routines", None) and b.registry.get("field") is not None:
        b.add_routine_set("before_scan", "field", 190.0)
        b.set_routine_action("before_scan", "vna_reference")
        b.add_routine_set("after_scan", "field", 0.0)
    # ...and one THROUGHOUT: autofocus before every row of the map.
    if hasattr(b, "add_throughout") and b.registry.get_action("sim_autofocus"):
        b.add_throughout().set_action("sim_autofocus")
    b._rebuild_summary()


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


def _mission_control(theme):
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


TARGETS = {
    "pm16": _pm16,
    "ppms": _ppms,
    "mag2d": _mag2d,
    "mag2dcal": _mag2dcal,
    "vna": _vna,
    "suite-control": _suite("Control", _tick_some_controls, settle=4.0, follow=True),
    "suite-scan": _suite("Scan", _stack_two_axes, settle=3.0),
    "suite-measurement": _suite("Measurement", _scan_then_show_run, settle=3.5),
    "suite-data": _suite("Data", _scan_3d_then_show_data, settle=3.5),
    "suite-settings": _suite("Settings", settle=2.0),
    "suite-queue": _suite("Measurement", _queue_running, settle=2.5),
    "suite-queue-dialog": _suite("Measurement", _queue_dialog, settle=1.0),
    "viewer-map": _viewer("map"),
    "viewer-1d": _viewer("1d"),
    "clMag": _clMag,
    "smb": _smb,
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
