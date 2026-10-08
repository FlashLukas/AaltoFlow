""""Start for a data file...": the mapping (no Qt), the helper call, the
dialog and the start path.

No real scan-core and no .nc: the helper's JSON answer is faked, the way
scan-core's own test (tests/test_file_modules.py) pins what it prints.

Named after test_launcher.py ON PURPOSE: mission_control reads the suite root
once, at import, and test_launcher's fixture expects to be the importer.
This file patches the module's globals for its own temp suite instead, and
puts them back afterwards.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from suite_common import discover  # noqa: E402

import datafile_start as DS  # noqa: E402  (no Qt in it)

#: ports far from the suite's 5555... and from test_launcher's 161xx
BASE = 16230
REMOTE_HOST = "192.0.2.1"        # TEST-NET-1: never a real PC


def _module(root: Path, key, cmd, after=(), gui=True, order=10):
    d = root / "modules" / "other" / f"{key}-control"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("", encoding="utf-8")
    if gui:
        (d / "scripts" / "run_gui.py").write_text("", encoding="utf-8")
    after_txt = ", ".join(f'"{k}"' for k in after)
    (d / "module.toml").write_text(
        f'[module]\nkey = "{key}"\nname = "{key} module"\ndescription = "fake"\n'
        f'order = {order}\n[ports]\ncmd = {cmd}\npub = {cmd + 1}\n'
        f'[run]\nservice = "scripts/run_service.py"\n'
        f'gui = "{"scripts/run_gui.py" if gui else ""}"\nstart_after = [{after_txt}]\n',
        encoding="utf-8")


def _suite(root: Path) -> Path:
    # "cam" lists "stage" (a camera that drives a stage): stage must start first
    # although cam comes first in the list (order 5)
    _module(root, "cam", BASE, after=("stage",), order=5)
    _module(root, "stage", BASE + 2, order=10)
    _module(root, "lockin", BASE + 4, order=20)
    _module(root, "focus", BASE + 6, gui=False, order=30)
    (root / "suite_local.json").write_text(json.dumps({
        "modules": {"stage": {"real": True}},
        "remote": [{"host": REMOTE_HOST, "cmd": BASE + 10, "pub": BASE + 11,
                    "key": "lockin"}],
        "settings": {}}), encoding="utf-8")
    return root


#: what scan_core.file_modules prints for a file measured with: cam + stage +
#: a lock-in on another PC (lockin_192_0_2_1, which has a remote card here),
#: a magnet this PC does not have, and a lock-in on a THIRD PC (no card).
RESULT = {"file": "C:/data/2026-10-08/101500_map.nc", "source": "snapshot", "modules": [
    {"slug": "cam", "key": "cam", "real": False, "idn_model": "SimCamera",
     "source": "snapshot", "in_recipe": True},
    {"slug": "stage", "key": "stage", "real": False, "idn_model": "SIM stage",
     "source": "snapshot", "in_recipe": True},
    {"slug": "lockin_192_0_2_1", "key": "lockin", "real": True,
     "idn_model": "Zurich Instruments HF2LI", "source": "snapshot", "in_recipe": True},
    {"slug": "magnet", "key": "magnet", "real": True, "idn_model": "KEPCO BOP",
     "source": "snapshot", "in_recipe": False},
    {"slug": "lockin_lab9", "key": "lockin", "real": None, "idn_model": "",
     "source": "recipe", "in_recipe": True},
]}


# ───────────────────────────── the mapping (no Qt) ───────────────────────────

def test_rows_match_cards_and_say_what_is_missing(tmp_path):
    found = discover(_suite(tmp_path))
    rows = {r.slug: r for r in DS.plan_rows(RESULT, found.modules,
                                            up={"cam": True}, owned=set())}
    assert list(rows) == [m["slug"] for m in RESULT["modules"]]      # the file's order

    cam = rows["cam"]                     # up, but not started by this launcher
    assert cam.card_id == "cam" and cam.state_kind == DS.UP_ELSEWHERE
    assert not cam.ticked and cam.tick_enabled

    stage = rows["stage"]                 # stopped, local: ticked by default
    assert stage.state_kind == DS.STOPPED and stage.ticked and stage.can_start
    # the file used the SIMULATOR, the card is set to real: said, not changed
    assert stage.file_real is False and stage.card_real is True
    assert "SIMULATOR" in stage.warning and "real hardware" in stage.warning

    rem = rows["lockin_192_0_2_1"]        # matched to the remote card by slug
    assert rem.state_kind == DS.REMOTE and rem.card_id.startswith("lockin@192.0.2.1")
    assert not rem.can_start and not rem.ticked and rem.warning == ""

    mag = rows["magnet"]                  # not installed here
    assert mag.state_kind == DS.MISSING and mag.card_id is None
    assert not mag.tick_enabled and "not installed on this PC" in mag.note
    assert "Add module" in mag.note

    other = rows["lockin_lab9"]           # another PC, no card for it here
    assert other.state_kind == DS.OTHER_PC and not other.tick_enabled
    assert "Add remote" in other.note and "lockin module" in other.note
    assert other.key == "lockin"


def test_real_mismatch_both_ways_and_running_state(tmp_path):
    found = discover(_suite(tmp_path))
    res = {"modules": [{"slug": "cam", "key": "cam", "real": True},
                       {"slug": "lockin", "key": "lockin", "real": None}]}
    rows = {r.slug: r for r in DS.plan_rows(res, found.modules,
                                            up={"cam": True}, owned={"cam"})}
    assert rows["cam"].state_kind == DS.RUNNING and rows["cam"].state == "running here"
    assert "REAL instrument" in rows["cam"].warning and "simulation" in rows["cam"].warning
    assert rows["lockin"].warning == ""                   # the file cannot tell
    assert DS.mismatch_text(True, True) == DS.mismatch_text(False, False) == ""


def test_an_old_file_guess_of_the_key_is_refined_by_what_this_pc_knows():
    # a key with an underscore cannot be split from the slug blindly
    assert DS.resolve_key("my_dev_lab2", "my", {"my_dev", "kim"}) == "my_dev"
    assert DS.resolve_key("kim", "kim", {"kim"}) == "kim"
    assert DS.resolve_key("hf2_lab2", "hf2", set()) == "hf2"


# ───────────────────────────── the helper call ────────────────────────────────

class _Done:
    def __init__(self, out=b"", err=b"", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def _fake_scan_core(root: Path) -> Path:
    py = root / "scan-core" / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    return py


def test_the_helper_runs_in_scan_cores_environment_and_reads_the_last_json(tmp_path):
    py = _fake_scan_core(tmp_path)
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw.get("cwd"), timeout=kw.get("timeout"))
        return _Done(b"some library notice\n" + json.dumps(RESULT).encode() + b"\n")

    out = DS.run_helper(tmp_path, "x.nc", runner=runner)
    assert out["modules"] == RESULT["modules"]
    assert seen["cmd"] == [str(py), "-m", "scan_core.file_modules", "x.nc"]
    assert Path(seen["cwd"]) == tmp_path / "scan-core" and seen["timeout"] > 0


def test_helper_failures_come_back_as_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))   # not this PC's venvs
    # no scan-core and no uv: said, not raised
    out = DS.run_helper(tmp_path, "x.nc", find_uv=lambda: None)
    assert out["modules"] == [] and "scan-core is not installed" in out["error"]

    _fake_scan_core(tmp_path)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    assert "longer than" in DS.run_helper(tmp_path, "x.nc", runner=slow, timeout_s=3)["error"]

    def crash(cmd, **kw):
        return _Done(b"", b"Traceback ...\nModuleNotFoundError: xarray\n", 1)
    err = DS.run_helper(tmp_path, "x.nc", runner=crash)["error"]
    assert "no answer" in err and "xarray" in err

    # the helper's own error ("not a scan-core file") is passed through
    def says_no(cmd, **kw):
        return _Done(json.dumps({"file": "x.nc", "modules": [], "error": "names no module"}).encode())
    assert DS.run_helper(tmp_path, "x.nc", runner=says_no)["error"] == "names no module"


def test_without_a_venv_uv_runs_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))   # not this PC's venvs
    assert DS.helper_command(tmp_path, find_uv=lambda: "uv.exe") is None   # no scan-core
    (tmp_path / "scan-core").mkdir()
    cmd = DS.helper_command(tmp_path, find_uv=lambda: "uv.exe")
    assert cmd[0] == "uv.exe"
    assert cmd[1][:3] == ["run", "--project", str(tmp_path / "scan-core")]
    assert cmd[1][-2:] == ["-m", "scan_core.file_modules"]


# ───────────────────────────── the window ────────────────────────────────────

@pytest.fixture(scope="module")
def win_env(tmp_path_factory):
    pytest.importorskip("PySide6")
    root = _suite(tmp_path_factory.mktemp("dfsuite"))
    prev_env = os.environ.get("AALTOFLOW_ROOT")
    imported_here = "mission_control" not in sys.modules
    os.environ["AALTOFLOW_ROOT"] = str(root)
    import mission_control as mc
    from PySide6 import QtWidgets
    saved = {k: getattr(mc, k) for k in ("ROOT", "CACHE_DIR", "PROFILES_FILE")}
    mc.ROOT, mc.CACHE_DIR = root, root / ".suite_cache"
    mc.PROFILES_FILE = root / "profiles.json"          # never the repo's own file
    mc.set_theme("dark")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = mc.MainWindow()
    yield mc, win, root, app
    win.close()
    for k, v in saved.items():
        setattr(mc, k, v)
    if prev_env is None:
        os.environ.pop("AALTOFLOW_ROOT", None)
    else:
        os.environ["AALTOFLOW_ROOT"] = prev_env
    if imported_here:                  # the next importer reads ITS root again
        sys.modules.pop("mission_control", None)


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_the_button_and_drops_are_there(win_env):
    mc, win, root, app = win_env
    assert "Start for a data file" in win.file_btn.text()
    assert win.acceptDrops()


def test_the_dialog_lists_every_module_and_greys_what_cannot_start(win_env):
    mc, win, root, app = win_env
    dlg = win.show_file_modules(RESULT["file"], RESULT, modal=False)
    assert dlg.table.rowCount() == 5
    texts = [dlg.table.item(r, 1).text() for r in range(5)]
    assert texts[0].startswith("cam module") and "connected, not scanned" in texts[3]
    from PySide6 import QtCore
    # magnet (not installed) and lockin_lab9 (other PC): not tickable
    for r in (3, 4):
        assert not (dlg.table.item(r, 0).flags() & QtCore.Qt.ItemIsEnabled)
    # the default ticks: the stopped local modules (cam, stage)
    assert set(dlg.ticked_ids()) == {"cam", "stage"}
    assert "SIMULATOR" in dlg.table.item(1, 6).text()     # stage's mismatch
    assert dlg.warn.text() and dlg.start_btn.isEnabled()
    dlg.set_ticked([])
    assert not dlg.start_btn.isEnabled()
    # a profile of the file's setup is useful even with nothing to start
    assert dlg.profile_btn.isEnabled()
    assert set(dlg.profile_ids()) == {r.card_id for r in dlg.rows if r.card_id}
    assert dlg.recall is True                 # recall offered by default
    dlg.set_ticked(["cam"])
    dlg.choose("start_guis")
    from PySide6 import QtWidgets
    assert dlg.action == "start_guis"
    assert dlg.result() == QtWidgets.QDialog.DialogCode.Accepted
    dlg.deleteLater()


def test_start_ticked_follows_the_start_order_and_opens_no_gui(win_env, monkeypatch):
    mc, win, root, app = win_env
    started, guis = [], []
    for mid, card in win.cards.items():
        monkeypatch.setattr(card, "start_service", lambda m=mid: started.append(m))
        monkeypatch.setattr(card, "open_gui", lambda m=mid: guis.append(m))
    # cam is listed (and ticked) first, but it starts after stage (start_after);
    # start_order places everything ready in one pass, so focus comes before cam
    win.apply_file_choice("start", ["cam", "stage", "focus"], "map")
    _pump(app, 1.8)
    assert started == ["stage", "focus", "cam"]
    assert guis == []
    # with GUIs: the same start, then every ticked module that has a GUI
    started.clear()
    win.apply_file_choice("start_guis", ["cam", "stage", "focus"], "map")
    _pump(app, 3.6)
    assert started == ["stage", "focus", "cam"]
    assert sorted(guis) == ["cam", "stage"]                # focus has no GUI


def test_the_real_box_is_never_touched(win_env, monkeypatch):
    mc, win, root, app = win_env
    for card in win.cards.values():
        monkeypatch.setattr(card, "start_service", lambda: None)
    before = {mid: c.spec.real for mid, c in win.cards.items()}
    dlg = win.show_file_modules(RESULT["file"], RESULT, modal=False)
    win.apply_file_choice("start", dlg.ticked_ids(), "map")
    _pump(app, 1.2)
    assert {mid: c.spec.real for mid, c in win.cards.items()} == before
    assert json.loads((root / "suite_local.json").read_text("utf-8"))["modules"]["stage"]["real"]
    dlg.deleteLater()


def test_save_as_profile_makes_a_chip(win_env):
    mc, win, root, app = win_env
    rid = next(m.id for m in win.found.modules if m.remote)
    win.apply_file_choice("profile", ["stage", "cam", rid], "map", profile_name="map rig")
    saved = json.loads((root / "profiles.json").read_text("utf-8"))
    assert {"name": "map rig", "members": ["stage", "cam", rid]} in saved
    names = [win.profile_bar.itemAt(i).widget().text() for i in range(win.profile_bar.count())]
    assert "map rig" in names
    # saving again under the same name replaces it, never a second chip
    win.apply_file_choice("profile", ["stage"], "map", profile_name="map rig")
    assert [p["members"] for p in win.profiles if p["name"] == "map rig"] == [["stage"]]


def test_the_file_is_read_in_the_background(win_env, monkeypatch):
    """The helper runs in a thread; the dialog comes up from the GUI thread."""
    mc, win, root, app = win_env
    if not (root / "scan-core").exists():
        _fake_scan_core(root)              # the runner is fake; the path must exist
    shown = []
    monkeypatch.setattr(win, "show_file_modules", lambda p, r: shown.append((p, r)))

    def runner(cmd, **kw):
        time.sleep(0.3)                    # a slow helper must not block the window
        return _Done(json.dumps(RESULT).encode())

    t0 = time.monotonic()
    assert win.start_for_data_file("C:/data/map.nc", runner=runner)
    assert time.monotonic() - t0 < 0.25
    assert not win.file_btn.isEnabled()
    assert not win.start_for_data_file("C:/data/other.nc", runner=runner)   # one at a time
    deadline = time.monotonic() + 10
    while not shown and time.monotonic() < deadline:
        _pump(app, 0.05)
    assert shown and shown[0][0] == "C:/data/map.nc"
    assert shown[0][1]["modules"] == RESULT["modules"]
    assert win.file_btn.isEnabled()


def test_a_dropped_nc_starts_the_same_path(win_env, monkeypatch):
    mc, win, root, app = win_env
    from PySide6 import QtCore, QtGui
    got = []
    monkeypatch.setattr(win, "start_for_data_file", lambda p=None: got.append(p))
    mime = QtCore.QMimeData()
    nc = root / "dropped.nc"
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(nc))])
    ev = QtGui.QDropEvent(QtCore.QPointF(10, 10), QtCore.Qt.CopyAction, mime,
                          QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    win.dropEvent(ev)
    assert got and Path(got[0]) == nc
    # anything else is not taken
    mime2 = QtCore.QMimeData()
    mime2.setUrls([QtCore.QUrl.fromLocalFile(str(root / "notes.txt"))])
    ev2 = QtGui.QDropEvent(QtCore.QPointF(10, 10), QtCore.Qt.CopyAction, mime2,
                           QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    win.dropEvent(ev2)
    assert len(got) == 1


def test_an_unreadable_file_is_logged_not_shown(win_env):
    mc, win, root, app = win_env
    assert win.show_file_modules("x.nc", {"modules": [], "error": "cannot read"},
                                 modal=False) is None
    assert "cannot read" in win.logbox.toPlainText()


def test_recall_opens_the_suite_with_the_file(win_env, monkeypatch):
    """Lukas 2026-10-08: "add that" -- after the start, the measurement suite
    opens with --recall FILE (its own list; nothing is sent from here)."""
    mc, win, root, app = win_env
    asked = []
    monkeypatch.setattr(win, "open_suite", lambda recall=None: asked.append(recall))
    for card in win.cards.values():
        monkeypatch.setattr(card, "start_service", lambda: None)
    win.apply_file_choice("recall", [], "map", path="C:/data/map.nc")
    assert asked == ["C:/data/map.nc"]
    asked.clear()
    win.apply_file_choice("start", ["stage"], "map", path="C:/data/map.nc", recall=True)
    assert asked == []                         # not before the services had time
    _pump(app, 3.0)
    assert asked == ["C:/data/map.nc"]
    asked.clear()
    win.apply_file_choice("start", ["stage"], "map", path="C:/data/map.nc", recall=False)
    _pump(app, 3.0)
    assert asked == []


def test_the_suite_gets_the_recall_argument(win_env, monkeypatch):
    mc, win, root, app = win_env
    seen = {}

    def fake_open_app(script, tag, what, running, extra_args=None):
        seen["args"] = list(extra_args or [])
        return None
    monkeypatch.setattr(win, "_open_app", fake_open_app)
    win.suite_proc = None
    win.open_suite(recall="C:/data/map.nc")
    assert seen["args"] == ["--recall", "C:/data/map.nc"]
