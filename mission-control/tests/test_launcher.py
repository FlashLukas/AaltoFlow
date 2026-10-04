"""The launcher against a throwaway suite folder, offscreen.

AALTOFLOW_ROOT points discovery at a temporary root BEFORE mission_control is
imported (the module reads the root once, at import). A tiny ZeroMQ REP thread
plays a running service that answers `describe`, so the remote-service path is
tested over a real socket, not a mock.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

zmq = pytest.importorskip("zmq")
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parents[1]


def _module(root: Path, folder, key, cmd, gui=True, order=10):
    d = root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    if gui:
        (d / "scripts" / "run_gui.py").write_text("")
    (d / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        '<circle cx="20" cy="20" r="10" fill="#ff9e2c"/></svg>')
    (d / "module.toml").write_text(
        f'[module]\nkey = "{key}"\nname = "{key} module"\ndescription = "fake"\n'
        f'icon = "icon.svg"\norder = {order}\n[ports]\ncmd = {cmd}\npub = {cmd + 1}\n'
        f'[run]\nservice = "scripts/run_service.py"\n'
        f'gui = "{"scripts/run_gui.py" if gui else ""}"\n')


class FakeService:
    """Answers `describe` like a module called `key`."""

    def __init__(self, port, key="lockin"):
        self.port, self.key = port, key
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        time.sleep(0.1)

    def _run(self):
        rep = zmq.Context.instance().socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(50):
                msg = rep.recv_json()
                if msg.get("cmd") == "describe":
                    rep.send_json({"ok": True, "describe": {
                        "module": self.key, "label": "Fake lock-in", "revision": 1,
                        "parameters": [
                            {"id": "tc1", "label": "Ch1 time constant", "kind": "control",
                             "type": "float", "unit": "ms", "min": 0.01, "max": 500},
                            {"id": "r1", "label": "Ch1 R", "kind": "indicator",
                             "type": "float", "unit": "V"},
                            {"id": "acquire", "label": "Acquire", "kind": "action",
                             "type": "action"}]}})
                else:
                    rep.send_json({"ok": False, "error": "unknown"})
        rep.close(0)

    def stop(self):
        self._stop.set()
        self._t.join(1)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("suite")
    _module(root, "modules/other/magnet-control", "magnet", 16100, order=10)
    _module(root, "modules/other/lockin-control", "lockin", 16110, order=20)
    _module(root, "modules/other/focus-control", "focus", 16120, gui=False, order=30)
    os.environ["AALTOFLOW_ROOT"] = str(root)
    sys.path.insert(0, str(HERE))
    import mission_control as mc
    from PySide6 import QtWidgets
    mc.set_theme("dark")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = mc.MainWindow()
    yield mc, win, root, app
    win.close()


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_cards_come_from_the_folder(env):
    mc, win, root, app = env
    assert list(win.cards) == ["magnet", "lockin", "focus"]
    assert not win.cards["focus"].btn_gui.isEnabled()          # headless
    assert win.cards["magnet"].btn_service.isEnabled()


def test_measurement_suite_button_is_not_a_module_card(env):
    """scan-core has no service and no ports, so it is a button, not a card --
    and it says so instead of failing silently when it is not there."""
    mc, win, root, app = env
    assert "scan-core" not in win.cards and "scan_core" not in win.cards
    assert win.suite_btn.isEnabled()

    win.open_suite()                       # the throwaway root has no scan-core
    assert win.suite_proc is None
    assert "no measurement suite at" in win.logbox.toPlainText()
    win.open_viewer()                      # the data viewer lives in scan-core too
    assert win.viewer_proc is None
    assert "no data viewer at" in win.logbox.toPlainText()


def test_a_new_module_folder_appears_on_rescan(env):
    mc, win, root, app = env
    _module(root, "modules/other/vna-control", "vna", 16130, order=40)
    win.rescan(force=False)                                     # signature changed
    assert "vna" in win.cards
    import shutil
    shutil.rmtree(root / "modules/other/vna-control")
    win.rescan(force=False)
    assert "vna" not in win.cards
    # a folder dropped straight into the root (the layout before modules/)
    # is still seen by the rescan -- it uses the same search as discovery
    _module(root, "scope-control", "scope", 16150, order=45)
    win.rescan(force=False)
    assert "scope" in win.cards
    shutil.rmtree(root / "scope-control")
    win.rescan(force=False)
    assert "scope" not in win.cards


def test_port_override_updates_the_card(env):
    mc, win, root, app = env
    mc.set_ports("magnet", 16200, 16201, root)
    win.rescan(force=True)
    card = win.cards["magnet"]
    assert (card.spec.cmd, card.spec.pub) == (16200, 16201)
    assert "changed" in card.meta.text()
    assert mc.service_args(card.spec)[:2] == ["--cmd-port", "16200"]
    mc.set_ports("magnet", None, None, root)
    win.rescan(force=True)
    assert win.cards["magnet"].spec.cmd == 16100


def test_remote_service_add_shows_variables_and_remove(env):
    mc, win, root, app = env
    svc = FakeService(16300, key="lockin")
    try:
        rid = mc.add_remote("127.0.0.1", 16300, 16301, "lockin", name="Lock-in lab 2", root=root)
        win.rescan(force=True)
        card = win.cards[rid]
        assert card.spec.remote and not card.btn_service.isVisible()
        assert card.spec.has_gui                                  # uses the local twin's GUI
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and card.manifest is None:
            _pump(app, 0.1)
        assert card.up, "the probe never saw the fake service"
        assert card.manifest and card.manifest["module"] == "lockin"
        assert card.btn_vars.text() == "Variables 2"              # 1 control + 1 measured
        assert card.vars.topLevelItemCount() == 3                 # controls, measured, actions
        assert (root / ".suite_cache").is_dir()                   # remembered for later
        mc.remove_remote(rid, root)
        win.rescan(force=True)
        assert rid not in win.cards
    finally:
        svc.stop()


def test_endpoints_are_handed_to_spawned_processes(env):
    mc, win, root, app = env
    table = json.loads(mc.endpoints_json(win.found.modules))
    assert table["lockin"] == ["localhost", 16110, 16111]


def test_profiles_keep_members_that_are_missing_today(env):
    mc, win, root, app = env
    cleaned = mc._clean_profiles([{"name": "Run", "members": ["magnet", "not-here"]},
                                  {"name": mc.FULL_SUITE, "members": []}])
    assert cleaned == [{"name": "Run", "members": ["magnet", "not-here"]}]


def test_icon_follows_the_theme(env):
    mc, win, root, app = env
    spec = win.found.get("magnet")
    pm = mc.module_icon(spec)
    assert not pm.isNull() and pm.width() == 40


# ─────────────────────────── Add module… (2026-09-24) ─────────────────────────

def test_add_module_from_a_folder(env, tmp_path):
    """Pick a folder of modules, see what each would do here, install the new one;
    the launcher shows its card without a restart."""
    mc, win, root, app = env
    shop = tmp_path / "shop"
    _module(shop, "pm-control", "pm", 16110, order=40)        # lockin's ports -> moved
    _module(shop, "magnet-control", "magnet", 16100)          # same folder: update
    _module(shop, "other-control", "lockin", 16200)           # key used elsewhere
    (shop / "pm-control" / "module.toml").write_text(
        (shop / "pm-control" / "module.toml").read_text().replace(
            "[module]\n", '[module]\ncategory = "detector"\ntags = ["power meter"]\n'))

    dlg = win.add_module()
    try:
        dlg.open_source(shop)
        rows = {dlg.tree.topLevelItem(i).data(0, mc.QtCore.Qt.UserRole):
                dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())}
        assert rows["pm"].text(1) == "Detectors & analyzers"
        assert rows["pm"].text(2).startswith("new (ports ")      # its ports were taken
        assert rows["magnet"].text(2) == "update"
        assert rows["lockin"].text(2) == "cannot install" and rows["lockin"].isDisabled()
        assert dlg.checked_keys() == ["pm"]                       # only NEW is pre-ticked

        dlg.cat_combo.setCurrentIndex(dlg.cat_combo.findData("detector"))
        assert dlg.tree.topLevelItemCount() == 1                  # "what do you need?"
        dlg.cat_combo.setCurrentIndex(0)

        dlg.install_checked(build=False)
        _pump(app, 0.2)
        assert (root / "modules/detector/pm-control" / "module.toml").is_file()
        assert "pm" in win.cards                                  # rescanned
        assert win.cards["pm"].spec.cmd not in (16100, 16110, 16120)
        assert mc.port_conflicts(win.found.modules) == []
    finally:
        dlg.close()


def test_add_module_runs_the_build_steps_in_order(env, tmp_path, monkeypatch):
    """The environment build is a chain of processes; a failing step stops that
    module's remaining steps and is reported, and the dialog is closable again."""
    mc, win, root, app = env
    shop = tmp_path / "shop"
    _module(shop, "scope-control", "scope", 16300)
    py = sys.executable
    fake = lambda folder, r, uv, offline: [                        # noqa: E731
        mc.catalog_Step("scope: one", [py, "-c", "print('step one')"], folder),
        mc.catalog_Step("scope: two", [py, "-c", "import sys; sys.exit(3)"], folder),
        mc.catalog_Step("scope: three", [py, "-c", "print('never')"], folder)]
    monkeypatch.setattr(mc, "env_steps", fake)
    monkeypatch.setattr(mc, "find_uv", lambda: "uv")
    dlg = win.add_module()
    try:
        dlg.open_source(shop)
        dlg.install_checked(build=True)
        assert not dlg.btn_close.isEnabled()                       # busy
        end = time.monotonic() + 20
        while dlg._proc is not None and time.monotonic() < end:
            _pump(app, 0.05)
        text = dlg.logbox.toPlainText()
        assert "step one" in text and "exit code 3" in text and "never" not in text
        assert "FAILED for scope" in text
        assert dlg.btn_close.isEnabled()
    finally:
        dlg.close()


def _serve(folder):
    import http.server
    import threading as th
    from functools import partial
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def _wait(app, cond, seconds=15):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        _pump(app, 0.05)
    assert cond(), "timed out"


def test_add_module_from_the_online_catalog(env, tmp_path):
    """The GitHub-release path, against a local HTTP server holding the same
    files a release has: catalog.json + one pack per module."""
    import json
    import zipfile
    from suite_common import catalog as K
    from suite_common.modules import discover_local
    mc, win, root, app = env
    src = tmp_path / "src"
    _module(src, "gauss-control", "gauss", 16400, order=5)
    (src / "gauss-control" / "module.toml").write_text(
        (src / "gauss-control" / "module.toml").read_text().replace(
            "[module]\n", '[module]\ncategory = "field"\ntags = ["gaussmeter"]\n'))
    rel = tmp_path / "release"
    rel.mkdir()
    zpath = rel / "gauss-1.0-abc.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in (src / "gauss-control").rglob("*"):
            zf.write(p, p.relative_to(src).as_posix())
    doc = K.release_catalog(discover_local(src)[0], {"gauss": {"code": zpath}},
                            "abc1234", "modules-test", None)
    assert "icon_svg" in doc["modules"][0]                  # the picture travels along
    (rel / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    httpd, url = _serve(rel)
    dlg = win.add_module()
    try:
        dlg.open_catalog(url + "catalog.json")
        _wait(app, lambda: dlg.catalog is not None)
        assert "modules-test" in dlg.src_lbl.text()
        it = dlg.tree.topLevelItem(0)
        assert (it.text(0), it.text(1), it.text(2)) == ("gauss module", "Magnetic field", "new")
        assert dlg.checked_keys() == ["gauss"]
        assert dlg.build_lbl.text().startswith("download 0.0 MB")   # a tiny fake pack

        dlg.install_checked(build=False)
        _wait(app, lambda: not dlg.busy and "gauss" in win.cards)
        log = dlg.logbox.toPlainText()
        assert "verified (SHA-256)" in log and "files copied" in log
        assert (root / "modules/field/gauss-control" / "module.toml").is_file()
        assert dlg.tree.topLevelItem(0).text(2) == "update"     # re-planned: now installed
    finally:
        dlg.close()
        httpd.shutdown()


def test_an_unreachable_catalog_is_explained_not_raised(env):
    mc, win, root, app = env
    dlg = win.add_module()
    try:
        dlg.open_catalog("http://127.0.0.1:9/catalog.json")
        _wait(app, lambda: not dlg.busy)
        assert dlg.catalog is None and "module pack" in dlg.problems.text()
        assert dlg.tree.topLevelItemCount() == 0
    finally:
        dlg.close()


def _running_proc(app, seconds):
    """A QProcess that is alive for `seconds` -- plays our just-started service."""
    from PySide6 import QtCore
    p = QtCore.QProcess()
    p.start(sys.executable, ["-c", f"import time; time.sleep({seconds})"])
    assert p.waitForStarted(5000)
    return p


def test_gui_right_after_service_waits_and_connects(env, monkeypatch):
    """Pressing GUI while our service is still starting must NOT open a private
    simulator: it waits for the port and then opens the GUI connected."""
    mc, win, root, app = env
    card = win.cards["magnet"]
    launched = []
    monkeypatch.setattr(card, "_launch_gui", lambda connect: launched.append(connect))
    card.up = False
    card.service_proc = _running_proc(app, 6)
    try:
        card.open_gui()
        _pump(app, 0.6)
        assert launched == []                       # still waiting: port closed
        svc = FakeService(16100, key="magnet")      # the service starts listening
        try:
            _pump(app, 1.5)
        finally:
            svc.stop()
        assert launched == [True]                   # opened, and connected
    finally:
        card.service_proc.kill(); card.service_proc.waitForFinished(3000)
        card.service_proc = None


def test_gui_does_not_open_when_the_service_dies_first(env, monkeypatch):
    mc, win, root, app = env
    card = win.cards["magnet"]
    launched = []
    monkeypatch.setattr(card, "_launch_gui", lambda connect: launched.append(connect))
    card.up = False
    card.service_proc = _running_proc(app, 0.5)
    card.open_gui()
    _pump(app, 2.0)
    assert launched == [] and not card._gui_waiting
    card.service_proc = None


def test_export_then_import_settings(env, tmp_path):
    """Export writes one zip; import restores a file tuned since, keeps a
    backup of what it replaced, and the cards follow an imported
    suite_local.json (here: the magnet's real-hardware flag)."""
    mc, win, root, app = env
    ini = root / "modules/other/magnet-control" / "magnet.ini"
    local = root / mc.LOCAL_FILE
    had_local = local.exists()
    old_local = local.read_bytes() if had_local else None
    try:
        ini.write_text("[hall]\noffset = 1\n", encoding="utf-8")
        mc.set_real("magnet", True, root)
        bundle = win.export_settings(str(tmp_path / "settings.zip"))
        assert bundle is not None and bundle.is_file()
        assert "exported" in win.logbox.toPlainText()

        ini.write_text("[hall]\noffset = 2\n", encoding="utf-8")
        mc.set_real("magnet", False, root)
        win.rescan(force=True)
        assert not win.cards["magnet"].spec.real

        plan = win.import_settings(str(bundle), confirm=False)
        assert {e.path for e in plan.overwrite} == {"modules/other/magnet-control/magnet.ini",
                                                    mc.LOCAL_FILE}
        assert "offset = 1" in ini.read_text(encoding="utf-8")
        assert win.cards["magnet"].spec.real                  # the card followed
        backups = list((root / ".suite_cache").glob("settings-backup-*.zip"))
        assert backups
        assert "backed up in" in win.logbox.toPlainText()
    finally:
        ini.unlink(missing_ok=True)
        if had_local:
            local.write_bytes(old_local)
        else:
            local.unlink(missing_ok=True)
        win.rescan(force=True)


def test_import_of_a_non_bundle_is_refused(env, tmp_path, monkeypatch):
    mc, win, root, app = env
    bad = tmp_path / "not.zip"
    bad.write_text("hello", encoding="utf-8")
    shown = []
    monkeypatch.setattr(mc.QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: shown.append(a[2]))
    assert win.import_settings(str(bad)) is None
    assert shown and "not a zip" in shown[0]


def test_exclusive_checkbox_is_unchanged(env):
    """The profile 'Exclusive' checkbox still exists and is off by default."""
    mc, win, root, app = env
    assert win.exclusive_check.text() == "Exclusive"
    assert not win.exclusive_check.isChecked()


# ───────────── physical addresses: who holds what (hwlock, 2026-09-27) ─────────────
# The lock itself lives in suite_common.hwlock and is claimed by each module's
# real backend. The launcher only SHOWS it. A claim made HERE, in the test
# process, plays a running service that holds the instrument; the lock folder
# is a temp folder so the real one on this PC is never touched.

@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(d))
    return d


def test_parse_busy_reads_hwlocks_refusal(env):
    mc, win, root, app = env
    line = ("suite_common.hwlock.HardwareBusy: GPIB0::6 is already in use by clMag "
            "(pid 4242) -- one instrument can be driven by one service at a time; stop that one first")
    assert mc.parse_busy(line) == {"address": "GPIB0::6", "holder": "clMag", "pid": 4242}
    assert mc.parse_busy("COM5 is already in use by another service -- x") == {
        "address": "COM5", "holder": "another service", "pid": None}
    assert mc.parse_busy("nothing to see") is None


def test_card_shows_the_address_its_service_holds(env, lockdir):
    """A claim by 'magnet' appears on magnet's card after one probe round,
    and on no other card; released, it disappears again."""
    mc, win, root, app = env
    from suite_common import hwlock
    lock = hwlock.claim("GPIB0::6::INSTR", "magnet")
    try:
        win.prober._round()                    # the same path the timer takes
        _pump(app, 0.1)
        mag, lockin = win.cards["magnet"], win.cards["lockin"]
        assert mag.hw.isVisibleTo(mag) and mag.hw.text() == "holds GPIB0::6"
        assert "GPIB0::6::INSTR" in mag.hw.toolTip()
        assert not lockin.hw.isVisibleTo(lockin)
    finally:
        lock.release()
    # A round the prober's own timer started BEFORE the release can still
    # deliver "holds GPIB0::6" after ours; under load (several suites at once)
    # it arrived after a single 0.1 s pump and the test failed now and then.
    # The card must clear within a few rounds, not necessarily the first.
    card = win.cards["magnet"]
    deadline = time.monotonic() + 3.0
    while card.hw.isVisibleTo(card) and time.monotonic() < deadline:
        win.prober._round()
        _pump(app, 0.1)
    assert not card.hw.isVisibleTo(card)


def test_holdings_match_by_pid_but_never_for_a_remote_card(env):
    mc, win, root, app = env
    spec = win.cards["lockin"].spec
    entries = [{"module": "something-else", "pid": 77, "normalized": "COM5"}]
    assert mc.holdings_for(spec, entries, pid=77) == entries
    assert mc.holdings_for(spec, entries, pid=None) == []
    import dataclasses
    assert mc.holdings_for(dataclasses.replace(spec, remote=True),
                           [{"module": "lockin", "pid": 1}]) == []


def test_a_service_refused_its_address_says_so_on_the_card(env, lockdir, monkeypatch):
    """A real child process tries to claim an address this test holds: it is
    refused by hwlock and exits. The card turns red with the holder's name
    instead of reporting a generic crash."""
    mc, win, root, app = env
    from suite_common import hwlock
    card = win.cards["focus"]
    script = root / "modules/other/focus-control/scripts/run_service.py"
    src = Path(hwlock.__file__).resolve().parents[1]
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from suite_common import hwlock\n"
        "hwlock.claim('GPIB::6', 'focus', wait_s=0.2)\n", encoding="utf-8")
    # No venv in the throwaway module: run the script with this very python.
    monkeypatch.setattr(mc, "build_command",
                        lambda d, s, extra, gui, prefer_venv=True: (sys.executable, [s, *extra]))
    lock = hwlock.claim("GPIB0::6", "magnet")
    try:
        card.start_service()
        end = time.monotonic() + 20
        while card.service_proc is not None and time.monotonic() < end:
            _pump(app, 0.05)
        assert card.service_proc is None, "the refused service should have exited"
        assert card.busy == {"address": "GPIB0::6", "holder": "magnet", "pid": os.getpid()}
        assert card.status_txt.text() == "address busy"
        assert card.hw.isVisibleTo(card)
        assert card.hw.text() == f"address busy: GPIB0::6 held by magnet (pid {os.getpid()})"
        log = win.logbox.toPlainText()
        assert "[focus] ADDRESS BUSY" in log and "[focus] NOT started: GPIB0::6 is held by magnet" in log
        assert "exited unexpectedly" not in log.split("[focus] ADDRESS BUSY")[-1]
    finally:
        lock.release()
        script.write_text("")
        card.busy = None
        card.show_hw()


# ---- card order (move the most-used modules to the top) --------------------

def _shown(mc, win):
    """Card ids top-to-bottom as they sit in the list on screen."""
    out = []
    for i in range(win.vlist.count()):
        w = win.vlist.itemAt(i).widget()
        if isinstance(w, mc.ModuleCard):
            out.append(w.spec.id)
    return out


def test_cards_can_be_moved_and_the_order_is_remembered(env):
    mc, win, root, app = env
    win.reset_card_order()
    own = [m.id for m in win.found.modules]          # the modules' own order
    assert _shown(mc, win) == own and len(own) >= 3
    a, b, c = own[0], own[1], own[2]
    # the top card cannot go further up, the bottom one not further down
    assert not win.cards[own[0]].btn_up.isEnabled() and win.cards[own[0]].btn_down.isEnabled()
    assert not win.cards[own[-1]].btn_down.isEnabled()

    win.cards[c].btn_up.click()                      # third card one place up
    assert _shown(mc, win)[:3] == [a, c, b]
    win.move_card(b, "top")
    assert _shown(mc, win)[:3] == [b, a, c]
    win.move_card(b, "bottom")
    assert _shown(mc, win)[-1] == b

    # remembered on this PC (suite_local.json) and survives a fresh rescan
    saved = json.loads((root / "suite_local.json").read_text("utf-8"))["settings"]["card_order"]
    assert saved == _shown(mc, win)
    win.rescan(force=True)
    assert _shown(mc, win) == saved
    # starting services does NOT follow the display order
    assert [x.spec.id for x in win.local_cards()] == [m.id for m in win.found.modules
                                                      if not m.remote]

    win.reset_card_order()
    assert _shown(mc, win) == own
    assert "card_order" not in json.loads(
        (root / "suite_local.json").read_text("utf-8")).get("settings", {})


def test_a_stale_or_partial_order_never_breaks_the_list(env):
    mc, win, root, app = env
    # an id that no longer exists is skipped; modules missing from the saved
    # list follow in their own order
    mc.set_setting(mc.CARD_ORDER_SETTING, ["gone-module", "focus"], root)
    win.layout_cards()
    shown = _shown(mc, win)
    own = [m.id for m in win.found.modules]
    assert shown[0] == "focus" and "gone-module" not in shown
    assert [x for x in shown if x != "focus"] == [x for x in own if x != "focus"]
    mc.set_setting(mc.CARD_ORDER_SETTING, "not a list", root)
    win.layout_cards()
    assert sorted(_shown(mc, win)) == sorted(win.cards)
    win.reset_card_order()


def test_a_long_description_wraps_to_two_lines_and_ends_with_dots(env):
    """Lukas 2026-09-29: long module descriptions pushed the card's buttons off
    the window. The description wraps to at most TWO lines, then '...'; the full
    text stays in the tooltip, and the label never asks for its full width."""
    mc, win, root, app = env
    card = win.cards["magnet"]
    long = ("Signal Hound SA44B / SA124B spectrum analyser; owner of the USB-TG44A "
            "tracking generator, which the shsg (CW) and shsna (TG sweeps) modules "
            "use through it -- and a lot more text so it can never fit in two lines "
            "of a narrow card whatever the font is on this computer, really")
    card.desc.setText(long)
    card.desc.resize(300, 80)
    app.processEvents()
    shown = card.desc.shown_text()
    lines = shown.split("\n")
    assert len(lines) <= 2 and lines[-1].endswith("…"), shown
    assert card.desc.toolTip() == long
    assert card.desc.minimumSizeHint().width() < 200       # never demands the full line
    card.desc.setText("short")
    card.desc.resize(300, 80)
    app.processEvents()
    assert card.desc.shown_text() == "short" and card.desc.toolTip() == ""


def test_instruments_dialog_lists_and_assigns_an_address(env, monkeypatch):
    """Instruments...: the scan's rows, the held one marked, and "Use for
    module..." writes the address for the module it fits (suite_local.json),
    which the service then gets with --real."""
    mc, win, root, app = env
    toml = root / "modules/other/lockin-control/module.toml"
    toml.write_text(toml.read_text() + '\n[hardware]\naddress_arg = "--resource"\nbus = "visa"\n')
    win.rescan(force=True)
    from suite_common import instruments as I
    rows = [I.Found("GPIB0::8::INSTR", "gpib", identity="Stanford_Research_Systems,SR830,1,1"),
            I.Found("GPIB0::6::INSTR", "gpib", held_by="kepco",
                    detail="held by a running service: not opened"),
            I.Found("COM5", "serial", identity="USB Serial Port (COM5)", detail="FTDI")]
    monkeypatch.setattr(mc.finder, "scan", lambda ask_visa=True: (list(rows), ["a note"]))
    monkeypatch.setattr(mc.finder, "scan_vendor", lambda *a, **k: ([], []))
    dlg = win.show_instruments()
    _pump(app, 0.5)
    try:
        assert dlg.table.rowCount() == 3
        # sorted by bus, then address: GPIB0::6 (kepco), GPIB0::8 (SR830), COM5
        assert dlg.table.item(0, 6).text() == "kepco"
        assert dlg.notes.text() == "a note"
        dlg.table.selectRow(2)                         # COM5: only a serial port may be asked
        assert dlg.ask_btn.isEnabled()
        # ... and a VISA module can take it under VISA's name for the port
        assert [a.text() for a in dlg.use_btn.menu().actions()] == \
            ["lockin module [lockin]: ASRL5::INSTR"]
        dlg.table.selectRow(1)                         # the SR830
        assert not dlg.ask_btn.isEnabled()
        actions = dlg.use_btn.menu().actions()
        assert [a.text() for a in actions] == ["lockin module [lockin]: GPIB0::8::INSTR"]
        actions[0].trigger()
        _pump(app, 0.2)
        win.rescan(force=True)
        spec = win.found.get("lockin")
        assert spec.address == "GPIB0::8::INSTR"
        mc.set_real("lockin", True, root)
        spec = mc.discover(root).get("lockin")
        assert mc.service_args(spec)[-2:] == ["--resource", "GPIB0::8::INSTR"]
        assert "lockin" in win.logbox.toPlainText() and "GPIB0::8::INSTR" in win.logbox.toPlainText()
    finally:
        dlg.close()
        mc.set_real("lockin", False, root)
        mc.set_address("lockin", None, root)


def test_instruments_dialog_shows_probe_and_usb_rows(env, monkeypatch):
    """Vendor-only devices: a module probe's row and USB-list rows (made-up
    serials). An unknown USB device is hidden until 'show every USB device';
    a probe's device is offered only to the module that found it, and the
    launcher passes it after --real like any other address."""
    mc, win, root, app = env
    d = root / "modules/other/focus-control"
    (d / "scripts" / "probe.py").write_text("")
    toml = d / "module.toml"
    toml.write_text(toml.read_text() + '\n[hardware]\naddress_arg = "--serial"\n'
                    'bus = "device"\nprobe = "scripts/probe.py"\n')
    win.rescan(force=True)
    from suite_common import instruments as I
    from suite_common import usb_devices as U
    probe_rows, _ = I.parse_probe(json.dumps({"devices": [
        {"address": "97000001", "identity": "Thorlabs KIM101", "detail": "Kinesis"}]}), "focus")
    usb = I.usb_rows([U.UsbDevice(0x0403, 0xFAF0, "APT USB Device", "USB", "97000001"),
                      U.UsbDevice(0x413C, 0x301A, "USB Input Device", "HIDClass", "")], held={})
    vendor = I.merge(probe_rows, usb)
    seen = {}

    def fake_vendor(modules, probes=True, python_for=None):
        seen["keys"] = sorted(m.key for m in modules if m.probe)
        return list(vendor), ["focus: a probe note"]
    monkeypatch.setattr(mc.finder, "scan", lambda ask_visa=True: ([], []))
    monkeypatch.setattr(mc.finder, "scan_vendor", fake_vendor)
    dlg = win.show_instruments()
    _pump(app, 0.6)
    try:
        assert seen["keys"] == ["focus"]
        assert dlg.table.rowCount() == 1                       # the keyboard is hidden
        assert dlg.table.item(0, 0).text() == "DEVICE"
        assert dlg.table.item(0, 4).text() == "focus probe + USB list"
        assert dlg.table.item(0, 5).text() == "focus module [focus]"
        assert "hidden" in dlg.status.text() and "a probe note" in dlg.notes.text()
        dlg.all_usb.setChecked(True)
        assert dlg.table.rowCount() == 2
        assert "not a known instrument" in dlg.table.item(1, 2).text()
        dlg.table.selectRow(0)
        actions = dlg.use_btn.menu().actions()
        assert [a.text() for a in actions] == ["focus module [focus]: 97000001"]
        assert not dlg.ask_btn.isEnabled()
        actions[0].trigger()
        _pump(app, 0.2)
        mc.set_real("focus", True, root)
        spec = mc.discover(root).get("focus")
        assert mc.service_args(spec)[-3:] == ["--real", "--serial", "97000001"]
        dlg.table.selectRow(1)                                 # the keyboard: nobody's
        assert not dlg.use_btn.isEnabled()
    finally:
        dlg.all_usb.setChecked(False)
        dlg.close()
        mc.set_real("focus", False, root)
        mc.set_address("focus", None, root)
