"""
AaltoFlow · Mission Control
========================

The launcher. It FINDS the suite's modules instead of listing them: every folder
next to this one that contains a `module.toml` is a module (see
suite-common/src/suite_common/modules.py). For each module it shows the name,
description and icon from that file, and -- once the service runs -- the
controls and measured variables the service reports through `describe`.

For each module you can:
  * start / stop its service (local modules), in real-hardware or simulator mode
  * open its GUI (connected to the live service when it is up)
  * change its ports on this PC
  * see its variables (fetched from the running service, remembered afterwards)

Services on OTHER PCs are added by hand ("Add remote...") and can be deleted
again. They get a card too, with a GUI button, but no Start/Stop: they belong
to whoever runs that PC.

Everything this PC chooses -- ports, real/sim, remote services -- is saved in
<root>/suite_local.json, which scan-core reads too. That file is how the
measurement suite "follows" the launcher.

Run it:
    cd mission-control
    uv run python mission_control.py [--theme light]
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from suite_common import get_setting, set_setting
from suite_common import (ENDPOINTS_ENV, PRODUCT, add_remote, default_root,
                          discover, endpoints_json, gui_args, probe,
                          remove_remote, service_args, set_ports, set_real,
                          setup_name, start_order, title as suite_title)
from suite_common.catalog import (DEFAULT_CATALOG_URL, CatalogError, InstallPlan,
                                  ModuleSource, asset_url, download, env_steps,
                                  fetch_catalog, install, module_version,
                                  plan_install, search, spec_from_entry)
from suite_common.catalog import Step as catalog_Step  # noqa: F401  (tests build steps)
from suite_common.catalog import find_uv as _find_uv
from suite_common.modules import (CATEGORIES, LOCAL_FILE, MANIFEST, ManifestError,
                                  ModuleSpec, port_conflicts)

# All colours come from theme.COLORS (aliased C); set_theme() swaps the palette
# IN PLACE at startup, so every C[...] read follows the active theme.
from theme import COLORS as C, DARK, set_theme, build_stylesheet, apply_palette  # noqa: E402

DEFAULT_THEME = "dark"

# ───────────────────────────── configuration ──────────────────────────────

#: The suite root: where the <module> folders live. AALTOFLOW_ROOT (or the old TRMOKE_ROOT) overrides it.
ROOT = default_root()

#: The last `describe` seen from each module, so the variables stay visible
#: while a service is down. Local machine state, not committed.
CACHE_DIR = ROOT / ".suite_cache"

# How we launch each project:
#   * SERVICES run with the project's OWN venv python (.venv\Scripts\python.exe)
#     when it exists, so the process we track IS the service and Stop can kill
#     it cleanly. `uv run` inserts a wrapper process, and killing the wrapper
#     can ORPHAN the real service, which keeps holding its port.
#   * GUIs run from the venv too when PySide6 is in it; otherwise `uv run
#     --extra gui`, which guarantees PySide6.
PREFER_VENV_PYTHON = False

PROBE_PERIOD_S = 1.2          # how often every service's port is checked
RESCAN_PERIOD_MS = 3000       # how often the folder is checked for new modules


def find_uv() -> str | None:
    """uv for THIS suite (suite_common.catalog.find_uv: PATH, <root>/uv, ~/.local/bin)."""
    return _find_uv(ROOT)


# ────────────────────────────────── profiles ──────────────────────────────
# A profile is a NAMED SUBSET of module ids you bring up together. Built-in
# defaults live here; profiles.json next to this script overrides them.
# Members that are not currently discovered are KEPT (the module may just be
# missing on this PC today) and skipped when the profile is activated.

DEFAULT_PROFILES = [
    dict(name="KIM rig",         members=["kim", "camera"]),
    dict(name="MOKE run",        members=["clMag", "zpiezo", "piezo", "camera", "hf2"]),
    dict(name="Alignment",       members=["stage", "kim", "zpiezo", "camera"]),
    dict(name="RF / pump-probe", members=["clMag", "smb", "camera", "hf2"]),
    # Two VNA-FMR chips because the two magnet modules drive the SAME coils and
    # must not both run: mag2d is the always-on PI, mag2dcal the calibrated seek
    # that freezes and stabilises. Tick "Exclusive" to swap one for the other.
    dict(name="VNA-FMR",         members=["mag2d", "vna"]),
    dict(name="VNA-FMR (cal)",   members=["mag2dcal", "vna"]),
    # The same measurement in the DynaCool: the cryostat's magnet (ppms) and
    # the VNA (the Copper Mountain on that PC, set in its vna.ini).
    dict(name="VNA-FMR (DynaCool)", members=["ppms", "vna"]),
]
FULL_SUITE = "Full suite"      # always present: every discovered local module

PROFILES_FILE = Path(__file__).resolve().parent / "profiles.json"


def _clean_profiles(raw) -> list[dict]:
    """Coerce arbitrary JSON into a safe list of {name, members} dicts."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        members = item.get("members", [])
        if not name or name == FULL_SUITE or not isinstance(members, list):
            continue
        out.append(dict(name=name, members=[str(k) for k in members]))
    return out


def load_profiles() -> list[dict]:
    """profiles.json if present & valid, else the built-in defaults (copied)."""
    if PROFILES_FILE.exists():
        try:
            data = _clean_profiles(json.loads(PROFILES_FILE.read_text("utf-8-sig")))
            if data:
                return data
        except (OSError, ValueError):
            pass
    return [dict(name=p["name"], members=list(p["members"])) for p in DEFAULT_PROFILES]


def save_profiles(profiles: list[dict]) -> None:
    PROFILES_FILE.write_text(json.dumps(_clean_profiles(profiles), indent=2) + "\n", "utf-8")


def migrate_launcher_json(log) -> None:
    """One-time move of the old per-card real flags into suite_local.json.

    Before discovery, real/sim lived in mission-control/launcher.json, which
    only the launcher could read. scan-core needs this PC's choices too, so they
    moved. launcher.json is left on disk untouched.
    """
    old = Path(__file__).resolve().parent / "launcher.json"
    if not old.exists() or (ROOT / LOCAL_FILE).exists():
        return
    try:
        flags = json.loads(old.read_text("utf-8-sig")).get("real", {})
    except (OSError, ValueError, AttributeError):
        return
    moved = [k for k, on in flags.items() if on]
    for key in moved:
        set_real(key, True, ROOT)
    if moved:
        log(f"moved real-hardware flags from launcher.json to {LOCAL_FILE}: {', '.join(moved)}")


# ─────────────────────────────────── icons ────────────────────────────────

def module_icon(spec: ModuleSpec, size: int = 40) -> QtGui.QPixmap:
    """The module's icon.svg in the ACTIVE theme, or a lettered placeholder.

    Icons are drawn in the dark palette's accent colours. Swapping those exact
    hex values for the active palette's makes one SVG work in both themes,
    without teaching every module author about theming.
    """
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    svg = None
    if spec.icon is not None:
        try:
            svg = spec.icon.read_text("utf-8")
        except OSError:
            svg = None
    if svg:
        for key in ("accent", "accent_hi", "text"):
            svg = re.sub(re.escape(DARK[key]), C[key], svg, flags=re.IGNORECASE)
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
        if renderer.isValid():
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            renderer.render(p, QtCore.QRectF(0, 0, size, size))
            p.end()
            return pm
    # placeholder: the first letter in a rounded square
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtGui.QPen(QtGui.QColor(C["accent"]), 2))
    p.drawRoundedRect(QtCore.QRectF(5, 5, size - 10, size - 10), 6, 6)
    f = p.font(); f.setBold(True); f.setPixelSize(int(size * 0.45)); p.setFont(f)
    p.drawText(QtCore.QRectF(0, 0, size, size), QtCore.Qt.AlignCenter,
               (spec.name or spec.key or "?")[:1].upper())
    p.end()
    return pm


def dot(color: str, d: int = 12) -> QtGui.QPixmap:
    pm = QtGui.QPixmap(d, d)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(color))
    p.drawEllipse(1, 1, d - 2, d - 2)
    p.end()
    return pm


# ─────────────────────── launching project scripts ─────────────────────────

def venv_python(project_dir: Path) -> Path | None:
    """The project's own interpreter, if its .venv has been created."""
    for cand in (project_dir / ".venv" / "Scripts" / "python.exe",
                 project_dir / ".venv" / "bin" / "python"):
        if cand.exists():
            return cand
    return None


def has_pyside(project_dir: Path) -> bool:
    site = project_dir / ".venv" / "Lib" / "site-packages" / "PySide6"
    return site.exists() or any((project_dir / ".venv" / "lib").glob("python*/site-packages/PySide6"))


#: The coordinator is NOT a module: it has no service and no ports, so it gets no
#: card (a Service button and a port for it would mean nothing, and the module
#: contract check would rightly flag them). It is one application that CONNECTS to
#: whatever services the launcher has running, so it lives in the global bar.
SUITE_DIR = "scan-core"
SUITE_SCRIPT = "apps/suite.py"
#: The data viewer (AaltoView's successor) lives in scan-core too. It reads files
#: and talks to no instrument, so it can be open while nothing else runs.
VIEWER_SCRIPT = "apps/viewer.py"


def build_command(project_dir: Path, script: str, extra: list[str], gui: bool,
                  prefer_venv: bool):
    """Return (program, args) to launch a project script."""
    py = venv_python(project_dir)
    if py is not None and (prefer_venv or PREFER_VENV_PYTHON or (gui and has_pyside(project_dir))):
        return str(py), [script, *extra]
    args = ["run", *(["--extra", "gui"] if gui else []), script, *extra]
    return find_uv() or "uv", args


# ───────────────────────── talking to services ────────────────────────────

def fetch_describe(host: str, port: int, timeout_ms: int = 1500) -> dict | None:
    """Ask a service for its `describe` manifest; None if it does not answer.

    A fresh REQ socket per call, closed at once: a REQ socket that timed out is
    stuck for good, so reusing one would break every later request.
    """
    import zmq
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(f"tcp://{host}:{int(port)}")
        sock.send_json({"cmd": "describe"})
        reply = sock.recv_json()
        return reply.get("describe") if reply.get("ok") else None
    except Exception:
        return None
    finally:
        sock.close(0)


def request_shutdown(host: str, port: int, timeout_ms: int = 1000) -> bool:
    """Ask a service to stop ITSELF; True if it agreed.

    Why not just kill it: on Windows QProcess.terminate() cannot reach a console
    program, so Stop always ended in a hard kill, and a hard kill gives the
    service no chance to close its hardware. The PM16 power meter is inside a
    USB read almost all the time, and a kill there left it answering "I/O
    error" until it was unplugged (2026-09-15). A service that knows the
    `shutdown` verb closes its instrument and exits; one that does not answers
    "unknown command" and is killed as before.
    """
    import zmq
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(f"tcp://{host}:{int(port)}")
        sock.send_json({"cmd": "shutdown"})
        return bool(sock.recv_json().get("ok"))
    except Exception:
        return False
    finally:
        sock.close(0)


def _cache_path(module_id: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", module_id) + ".json")


def save_cached_describe(module_id: str, manifest: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _cache_path(module_id).write_text(
            json.dumps({"time": time.time(), "describe": manifest}), "utf-8")
    except OSError:
        pass


def load_cached_describe(module_id: str) -> tuple[dict | None, float | None]:
    try:
        data = json.loads(_cache_path(module_id).read_text("utf-8"))
        return data.get("describe"), data.get("time")
    except (OSError, ValueError):
        return None, None


class Bridge(QtCore.QObject):
    """Background threads report here; Qt delivers it on the GUI thread."""
    probed = QtCore.Signal(dict)                  # id -> up?
    described = QtCore.Signal(str, object)        # id, manifest or None


class Prober:
    """Checks every module's command port on a background thread.

    A probe of an unreachable REMOTE host can take the whole timeout, and eight
    of those on the GUI thread would freeze the window. So the window hands the
    list of (id, host, port) over, and gets the answers back as a signal.
    """

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self._targets: list[tuple[str, str, int]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy: set[str] = set()
        self._thread = threading.Thread(target=self._loop, name="probe", daemon=True)
        self._thread.start()

    def set_targets(self, targets):
        with self._lock:
            self._targets = list(targets)

    def probe_now(self):
        """Run one probe round at once (after a start / rescan)."""
        threading.Thread(target=self._round, daemon=True).start()

    def describe(self, module_id: str, host: str, port: int):
        """Fetch one manifest in the background (one request per module at a time)."""
        with self._lock:
            if module_id in self._busy:
                return
            self._busy.add(module_id)

        def run():
            manifest = fetch_describe(host, port)
            with self._lock:
                self._busy.discard(module_id)
            self.bridge.described.emit(module_id, manifest)
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _round(self):
        with self._lock:
            targets = list(self._targets)
        if not targets:
            return
        # In PARALLEL: on Windows a probe of a closed port usually runs to the
        # full timeout, so eight modules one after another took ~2 s per round.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            ups = list(pool.map(lambda t: probe(t[1], t[2], 0.3), targets))
        result = {t[0]: up for t, up in zip(targets, ups)}
        if not self._stop.is_set():
            self.bridge.probed.emit(result)

    def _loop(self):
        while not self._stop.wait(PROBE_PERIOD_S):
            self._round()


# ─────────────────────────────────── dialogs ──────────────────────────────

class PortsDialog(QtWidgets.QDialog):
    """Change one local module's ports on this PC."""

    def __init__(self, spec: ModuleSpec, others: list[ModuleSpec], parent=None):
        super().__init__(parent)
        self.spec, self.others = spec, others
        self.setWindowTitle(f"Ports - {spec.name}")
        form = QtWidgets.QFormLayout(self)
        self.cmd = QtWidgets.QSpinBox(); self.cmd.setRange(1024, 65535); self.cmd.setValue(spec.cmd)
        self.pub = QtWidgets.QSpinBox(); self.pub.setRange(1024, 65535); self.pub.setValue(spec.pub)
        # pub follows cmd + 1 until someone edits pub by hand
        self._pub_follows = spec.pub == spec.cmd + 1
        self.cmd.valueChanged.connect(self._cmd_changed)
        self.pub.editingFinished.connect(lambda: setattr(self, "_pub_follows", False))
        form.addRow("command port (REQ/REP)", self.cmd)
        form.addRow("status port (PUB/SUB)", self.pub)
        hint = QtWidgets.QLabel(
            f"Default from {MANIFEST}: {spec.default_cmd} / {spec.default_pub}. "
            f"Saved on this PC only ({LOCAL_FILE}); the service uses it on its "
            f"next start, and scan-core and the other modules follow.")
        hint.setWordWrap(True); hint.setObjectName("meta")
        form.addRow(hint)
        self.error = QtWidgets.QLabel(""); self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{C['danger']};")
        form.addRow(self.error)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                        | QtWidgets.QDialogButtonBox.Cancel
                                        | QtWidgets.QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        bb.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(self._defaults)
        form.addRow(bb)
        self.result_ports: tuple[int | None, int | None] | None = None

    def _cmd_changed(self, v):
        if self._pub_follows:
            self.pub.setValue(min(65535, v + 1))

    def _defaults(self):
        self.cmd.setValue(self.spec.default_cmd)
        self.pub.setValue(self.spec.default_pub)

    def _save(self):
        cmd, pub = self.cmd.value(), self.pub.value()
        if cmd == pub:
            self.error.setText("The two ports must differ.")
            return
        probe_spec = ModuleSpec(id=self.spec.id, key=self.spec.key, name=self.spec.name,
                                host=self.spec.host, cmd=cmd, pub=pub)
        clashes = [c for c in port_conflicts([probe_spec] + self.others) if self.spec.id in c]
        if clashes:
            self.error.setText("; ".join(clashes))
            return
        default = (cmd, pub) == (self.spec.default_cmd, self.spec.default_pub)
        self.result_ports = (None, None) if default else (cmd, pub)
        self.accept()


class AddRemoteDialog(QtWidgets.QDialog):
    """Add a service that runs on another PC.

    "Test connection" asks the service to `describe` itself, which tells us
    what KIND of module it is -- and so which icon, description and GUI of the
    same module on this PC belong to it. If the other PC is off right now, the
    type can still be chosen by hand.
    """

    def __init__(self, local_modules: list[ModuleSpec], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a remote service")
        self.setMinimumWidth(460)
        self.local = {m.key: m for m in local_modules}
        form = QtWidgets.QFormLayout(self)

        self.host = QtWidgets.QLineEdit(); self.host.setPlaceholderText("e.g. 192.168.1.20 or lab2-pc")
        self.cmd = QtWidgets.QSpinBox(); self.cmd.setRange(1024, 65535); self.cmd.setValue(5555)
        self.pub = QtWidgets.QSpinBox(); self.pub.setRange(1024, 65535); self.pub.setValue(5556)
        self._pub_follows = True
        self.cmd.valueChanged.connect(lambda v: self._pub_follows and self.pub.setValue(min(65535, v + 1)))
        self.pub.editingFinished.connect(lambda: setattr(self, "_pub_follows", False))
        self.kind = QtWidgets.QComboBox()
        for m in sorted(local_modules, key=lambda m: m.order):
            self.kind.addItem(f"{m.name}  ({m.key})", m.key)
        self.name = QtWidgets.QLineEdit(); self.name.setPlaceholderText("optional, e.g. Lock-in (lab 2)")

        test = QtWidgets.QPushButton("Test connection")
        test.clicked.connect(self._test)
        self.result = QtWidgets.QLabel("Not tested yet."); self.result.setWordWrap(True)
        self.result.setObjectName("meta")

        form.addRow("host", self.host)
        form.addRow("command port", self.cmd)
        form.addRow("status port", self.pub)
        form.addRow("module type", self.kind)
        form.addRow("name", self.name)
        form.addRow(test, self.result)
        self.error = QtWidgets.QLabel(""); self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{C['danger']};")
        form.addRow(self.error)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.added_id: str | None = None

    def _test(self):
        host = self.host.text().strip()
        if not host:
            self.result.setText("Type a host first.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            manifest = fetch_describe(host, self.cmd.value())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not manifest:
            self.result.setText(f"No answer from {host}:{self.cmd.value()}. Is the service "
                                f"running and the port open in that PC's firewall? You can "
                                f"still add it: choose the module type by hand.")
            return
        key = manifest.get("module", "")
        n = len(manifest.get("parameters", []))
        idx = self.kind.findData(key)
        if idx < 0:
            self.kind.addItem(f"{manifest.get('label', key)}  ({key}, not installed here)", key)
            idx = self.kind.count() - 1
        self.kind.setCurrentIndex(idx)
        self.result.setText(f"Found '{manifest.get('label', key)}' (module {key}), "
                            f"{n} variables.")
        self._manifest = manifest

    def _ok(self):
        key = self.kind.currentData()
        if not key:
            self.error.setText("Choose the module type.")
            return
        try:
            self.added_id = add_remote(self.host.text().strip(), self.cmd.value(),
                                       self.pub.value(), key, name=self.name.text().strip(),
                                       root=ROOT)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        manifest = getattr(self, "_manifest", None)
        if manifest:
            save_cached_describe(self.added_id, manifest)
        self.accept()


class ProfileEditor(QtWidgets.QDialog):
    """Create / rename / delete profiles and toggle their members.

    Works on a private COPY; the caller reads it back only if accepted.
    """

    def __init__(self, profiles: list[dict], modules: list[ModuleSpec], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit profiles")
        self.resize(600, 420)
        self.profiles = [dict(name=p["name"], members=list(p["members"])) for p in profiles]
        self.modules = modules

        outer = QtWidgets.QVBoxLayout(self)
        body = QtWidgets.QHBoxLayout(); outer.addLayout(body, 1)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("Profiles"))
        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(self._load_members)
        left.addWidget(self.list, 1)
        lbtns = QtWidgets.QHBoxLayout()
        for text, slot in (("Add", self._add), ("Rename", self._rename), ("Remove", self._remove)):
            b = QtWidgets.QPushButton(text); b.clicked.connect(slot); lbtns.addWidget(b)
        left.addLayout(lbtns)
        body.addLayout(left, 1)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Members"))
        self.checks: dict[str, QtWidgets.QCheckBox] = {}
        for m in modules:
            where = f"remote {m.host}" if m.remote else (m.dir.name if m.dir else "")
            cb = QtWidgets.QCheckBox(f"{m.name}  ·  {where}")
            cb.toggled.connect(lambda on, k=m.id: self._toggle_member(k, on))
            self.checks[m.id] = cb
            right.addWidget(cb)
        right.addStretch(1)
        body.addLayout(right, 1)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._reload_list()
        if self.profiles:
            self.list.setCurrentRow(0)
        else:
            self._set_checks_enabled(False)

    def _current(self) -> dict | None:
        row = self.list.currentRow()
        return self.profiles[row] if 0 <= row < len(self.profiles) else None

    def _reload_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.profiles:
            self.list.addItem(f"{p['name']}  ({len(p['members'])})")
        self.list.blockSignals(False)

    def _set_checks_enabled(self, on: bool):
        for cb in self.checks.values():
            cb.setEnabled(on)

    def _load_members(self, _row: int):
        prof = self._current()
        self._set_checks_enabled(prof is not None)
        members = set(prof["members"]) if prof else set()
        for mid, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(mid in members)
            cb.blockSignals(False)

    def _toggle_member(self, mid: str, on: bool):
        prof = self._current()
        if prof is None:
            return
        if on and mid not in prof["members"]:
            prof["members"].append(mid)
        elif not on and mid in prof["members"]:
            prof["members"].remove(mid)
        row = self.list.currentRow()
        if 0 <= row < self.list.count():
            self.list.item(row).setText(f"{prof['name']}  ({len(prof['members'])})")

    def _add(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New profile", "Name:")
        name = name.strip()
        if ok and name and name != FULL_SUITE:
            self.profiles.append(dict(name=name, members=[]))
            self._reload_list()
            self.list.setCurrentRow(len(self.profiles) - 1)

    def _rename(self):
        prof = self._current()
        if prof is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename profile", "Name:", text=prof["name"])
        name = name.strip()
        if ok and name and name != FULL_SUITE:
            prof["name"] = name
            row = self.list.currentRow()
            self._reload_list()
            self.list.setCurrentRow(row)

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.profiles):
            del self.profiles[row]
            self._reload_list()
            if self.profiles:
                self.list.setCurrentRow(min(row, len(self.profiles) - 1))
            else:
                self._load_members(-1)

    def result_profiles(self) -> list[dict]:
        return self.profiles


# ──────────────────────────────── add module ───────────────────────────────

class AddModuleDialog(QtWidgets.QDialog):
    """Install modules from a folder, a module pack (.zip) or the ONLINE catalog.

    Online = the catalog.json of the newest GitHub release (tools/release_modules.py),
    or of a mirror of one (a copied release folder on a share). Nothing is
    downloaded until Install; every download is checked against the SHA-256 the
    catalog lists, then installed through the same path as a local pack.

    Pick a source, see what is in it -- what each module is FOR, and what
    installing it would do here (new / update / conflict) -- tick, Install. The
    code is copied at once; the Python environment is then built in the
    background, OFFLINE when the pack carries its packages (tools/pack_module.py
    --wheels), otherwise online with uv sync. The logic is all in
    suite_common.catalog; this dialog only shows it and runs the commands.

    An update never overwrites the rig's .ini / calibration files, and a new
    module whose ports are taken here gets a free pair on this PC.
    """

    installed = QtCore.Signal()                 # the launcher rescans on this
    # from worker threads (queued onto the GUI thread by Qt)
    _fetched = QtCore.Signal(object, str)        # catalog | None, error text
    _dl_note = QtCore.Signal(str)
    _dl_done = QtCore.Signal(object, object)     # [(key, zip path)], [errors]

    ACTION_TEXT = {"new": "new", "update": "update", "conflict": "cannot install",
                   "same": "already here"}

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add module")
        self.resize(900, 620)
        self.root = Path(root)
        self.source: ModuleSource | None = None
        self.plans: list[InstallPlan] = []
        self._queue: list = []                  # env build steps still to run
        self._proc: QtCore.QProcess | None = None
        self._failed: list[str] = []
        self.catalog: dict | None = None         # online mode when set
        self._busy_net = False                   # fetching or downloading
        self._cancel = False
        self._dl_tmp: tempfile.TemporaryDirectory | None = None
        self._fetched.connect(self._on_fetched)
        self._dl_note.connect(self.say)
        self._dl_done.connect(self._on_downloaded)

        v = QtWidgets.QVBoxLayout(self)
        src_row = QtWidgets.QHBoxLayout()
        b_dir = QtWidgets.QPushButton("From folder…")
        b_dir.setToolTip("A module folder, or a folder of module folders")
        b_dir.clicked.connect(self._pick_folder)
        b_zip = QtWidgets.QPushButton("From module pack (.zip)…")
        b_zip.setToolTip("Made with tools/pack_module.py -- with --wheels it installs "
                         "without internet")
        b_zip.clicked.connect(self._pick_zip)
        b_web = QtWidgets.QPushButton("From online catalog…")
        b_web.setToolTip("The newest published release on GitHub, or a mirror of one")
        b_web.clicked.connect(self._pick_online)
        src_row.addWidget(b_dir); src_row.addWidget(b_zip); src_row.addWidget(b_web)
        self.src_lbl = QtWidgets.QLabel("Choose where the modules come from.")
        self.src_lbl.setObjectName("meta"); self.src_lbl.setWordWrap(True)
        src_row.addWidget(self.src_lbl, 1)
        v.addLayout(src_row)

        flt = QtWidgets.QHBoxLayout()
        flt.addWidget(QtWidgets.QLabel("What do you need?"))
        self.cat_combo = QtWidgets.QComboBox()
        self.cat_combo.addItem("Anything", "")
        for key, (label, hint) in CATEGORIES.items():
            self.cat_combo.addItem(label, key)
            self.cat_combo.setItemData(self.cat_combo.count() - 1, hint, QtCore.Qt.ToolTipRole)
        self.cat_combo.currentIndexChanged.connect(self._fill)
        flt.addWidget(self.cat_combo)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("search: lock-in, Thorlabs, RF…")
        self.search.textChanged.connect(self._fill)
        flt.addWidget(self.search, 1)
        v.addLayout(flt)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Module", "Function", "On this PC", "Description"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QtCore.QSize(28, 28))
        self.tree.itemChanged.connect(lambda *_: self._sync_buttons())
        v.addWidget(self.tree, 3)

        self.problems = QtWidgets.QLabel(); self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color:{C['danger']};"); self.problems.hide()
        v.addWidget(self.problems)

        self.pkg_box = QtWidgets.QCheckBox(
            "Download the Python packages too (for a PC that cannot reach PyPI)")
        self.pkg_box.setToolTip("Downloads the '-offline' packs: much larger (PySide6 is "
                                "~250 MB), but the environment then builds with no internet")
        self.pkg_box.toggled.connect(lambda *_: self._sync_buttons())
        self.pkg_box.hide()
        v.addWidget(self.pkg_box)
        self.build_box = QtWidgets.QCheckBox("Build the Python environment now")
        self.build_box.setChecked(True)
        v.addWidget(self.build_box)
        self.build_lbl = QtWidgets.QLabel(); self.build_lbl.setObjectName("meta")
        self.build_lbl.setWordWrap(True)
        v.addWidget(self.build_lbl)

        self.logbox = QtWidgets.QPlainTextEdit(); self.logbox.setReadOnly(True)
        v.addWidget(self.logbox, 2)

        bb = QtWidgets.QHBoxLayout(); bb.addStretch(1)
        self.btn_install = QtWidgets.QPushButton("Install"); self.btn_install.setObjectName("primary")
        self.btn_install.clicked.connect(self.install_checked)
        self.btn_close = QtWidgets.QPushButton("Close"); self.btn_close.clicked.connect(self.close)
        bb.addWidget(self.btn_install); bb.addWidget(self.btn_close)
        v.addLayout(bb)
        self._sync_buttons()

    # ---- source -----------------------------------------------------------
    def _pick_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder with modules", str(Path.home()))
        if d:
            self.open_source(d)

    def _pick_zip(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Module pack", str(Path.home()),
                                                     "Module pack (*.zip)")
        if f:
            self.open_source(f)

    def _pick_online(self):
        url = get_setting("catalog_url", DEFAULT_CATALOG_URL, self.root)
        url, ok = QtWidgets.QInputDialog.getText(
            self, "Online catalog", "Catalog address (a GitHub release, or a mirror's "
            "catalog.json -- a URL or a path on a share):",
            QtWidgets.QLineEdit.Normal, url)
        if ok and url.strip():
            url = url.strip()
            # remembered on this PC only when it is not the default
            set_setting("catalog_url", None if url == DEFAULT_CATALOG_URL else url, self.root)
            self.open_catalog(url)

    def open_catalog(self, url: str) -> None:
        """Fetch a catalog in the background (a slow network must not freeze
        the launcher); the list fills when it arrives."""
        if self._busy_net:
            return
        self._busy_net = True
        self._sync_buttons()
        self.src_lbl.setText(f"fetching {url} …")

        def work():
            try:
                self._fetched.emit(fetch_catalog(url), "")
            except CatalogError as exc:
                self._fetched.emit(None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, doc, error: str) -> None:
        self._busy_net = False
        if doc is None:
            self.src_lbl.setText("online catalog: not available")
            self.problems.setText(error)
            self.problems.show()
            self._sync_buttons()
            return
        if self.source is not None:
            self.source.close()
            self.source = None
        self.catalog = doc
        entries = [e for e in doc.get("modules", []) if e.get("downloads")]
        self.plans = self._sorted(plan_install([self._online_spec(e) for e in entries],
                                               self.root))
        self.src_lbl.setText(f"online  ·  {doc.get('release', '?')}  ·  "
                             f"{len(entries)} modules  ·  {doc['_url']}")
        self.problems.hide()
        self.pkg_box.setVisible(any("offline" in e["downloads"] for e in entries))
        self._fill()

    @staticmethod
    def _online_spec(entry: dict) -> ModuleSpec:
        """spec_from_entry + the icon the release catalog carries inline."""
        spec = spec_from_entry(entry)
        svg = entry.get("icon_svg")
        if svg:
            d = Path(tempfile.gettempdir()) / "aaltoflow_icons"
            d.mkdir(exist_ok=True)
            f = d / f"{spec.key}.svg"
            f.write_text(svg, encoding="utf-8")
            spec.icon = f
        return spec

    def _entry(self, key: str) -> dict:
        return next(e for e in self.catalog["modules"] if e["key"] == key)

    def _kind(self, key: str) -> str:
        dls = self._entry(key)["downloads"]
        return "offline" if self.pkg_box.isChecked() and "offline" in dls else "code"

    def open_source(self, path) -> None:
        self.catalog = None
        self.pkg_box.hide()
        if self.source is not None:
            self.source.close()
        try:
            self.source = ModuleSource(path)
        except (ValueError, OSError) as exc:
            self.source, self.plans = None, []
            self.src_lbl.setText(f"Cannot read {Path(path).name}: {exc}")
            self._fill()
            return
        self.plans = self._sorted(plan_install(self.source.modules, self.root))
        pack = self.source.pack
        where = Path(path).name
        if pack:
            where += (f"  ·  pack of commit {pack.get('commit', '?')}"
                      + ("  ·  carries its packages (offline)" if pack.get("offline") else ""))
        self.src_lbl.setText(where)
        self.problems.setText("\n".join(self.source.problems))
        self.problems.setVisible(bool(self.source.problems))
        self._fill()

    # ---- list -------------------------------------------------------------
    @staticmethod
    def _sorted(plans):
        """Grouped by FUNCTION (the categories' own order), then the suite order:
        shopping for a detector, the detectors sit together."""
        rank = {k: i for i, k in enumerate(CATEGORIES)}
        return sorted(plans, key=lambda p: (rank[p.spec.category], p.spec.order, p.spec.name))

    def _fill(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        shown = search([p.spec for p in self.plans], self.search.text(),
                       self.cat_combo.currentData() or "")
        for plan in self.plans:
            if plan.spec not in shown:
                continue
            m = plan.spec
            action = self.ACTION_TEXT[plan.action]
            if plan.ports:
                action += f" (ports {plan.ports[0]}/{plan.ports[1]})"
            it = QtWidgets.QTreeWidgetItem([m.name, CATEGORIES[m.category][0], action,
                                            m.description])
            it.setIcon(0, QtGui.QIcon(module_icon(m, 28)))
            it.setData(0, QtCore.Qt.UserRole, m.key)
            tip = "\n".join(x for x in (
                f"{m.key}  ·  {m.dir.name}  ·  version "
                f"{m.version or module_version(m.dir) or '?'}",
                "tags: " + ", ".join(m.tags) if m.tags else "",
                plan.reason, *plan.notes) if x)
            for c in range(4):
                it.setToolTip(c, tip)
            if plan.installable:
                it.setCheckState(0, QtCore.Qt.Checked if plan.action == "new"
                                 else QtCore.Qt.Unchecked)
            else:
                it.setDisabled(True)
                it.setForeground(2, QtGui.QColor(C["danger"] if plan.action == "conflict"
                                                 else C["muted"]))
            self.tree.addTopLevelItem(it)
        for c in range(3):
            self.tree.resizeColumnToContents(c)
        self.tree.blockSignals(False)
        self._sync_buttons()

    def checked_keys(self) -> list[str]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == QtCore.Qt.Checked and not it.isDisabled():
                out.append(it.data(0, QtCore.Qt.UserRole))
        return out

    def set_checked(self, keys) -> None:
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if not it.isDisabled():
                it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole) in keys
                                 else QtCore.Qt.Unchecked)

    @property
    def busy(self) -> bool:
        return self._proc is not None or self._busy_net

    def _sync_buttons(self):
        keys = self.checked_keys()
        self.btn_install.setEnabled(bool(keys) and not self.busy)
        self.btn_close.setEnabled(self._proc is None)   # a download can be cancelled
        if self.catalog is not None and keys:
            size = sum(self._entry(k)["downloads"][self._kind(k)]["size"] for k in keys)
            if self.pkg_box.isChecked():
                how = "offline, from the downloaded packages"
            elif not self.pkg_box.isHidden():
                how = ("online with uv sync -- needs PyPI; tick the box above if this "
                       "PC cannot reach it")
            else:
                how = ("online with uv sync -- needs PyPI (this release has no offline "
                       "packs; without PyPI, install from a module pack made with --wheels)")
            self.build_lbl.setText(f"download {size / 1e6:.1f} MB; environments: {how}")
            return
        if self.source is None or not keys:
            self.build_lbl.setText("")
            return
        offline = [k for k in keys if self.source.offline_for(k)]
        online = [k for k in keys if k not in offline]
        bits = []
        if offline:
            bits.append(f"{', '.join(offline)}: offline, from the pack's own packages")
        if online:
            bits.append(f"{', '.join(online)}: online with uv sync -- needs internet "
                        "(PyPI). Without it, untick and use Start menu > Rebuild "
                        "Python environments later.")
        self.build_lbl.setText("   ".join(bits))

    # ---- install ----------------------------------------------------------
    def say(self, msg: str) -> None:
        self.logbox.appendPlainText(msg)

    def install_checked(self, build: bool | None = None) -> None:
        """Copy every ticked module, then (optionally) build their environments.
        Online: download first (background), then the same."""
        keys = self.checked_keys()
        if not keys or self.busy:
            return
        self._build = self.build_box.isChecked() if build is None else build
        if self.catalog is not None:
            self._download(keys)
            return
        if self.source is None:
            return
        done = self._install_from(self.source, keys)
        self.plans = self._sorted(plan_install(self.source.modules, self.root))
        self._fill()
        self._after_copy(done)

    def _install_from(self, source: ModuleSource, keys) -> list:
        """Copy the ticked modules of one source; returns [(plan, offline?)]."""
        done = []
        for plan in plan_install(source.modules, self.root):
            if plan.spec.key not in keys:
                continue
            try:
                for line in install(plan, self.root, wheels=source.wheels):
                    self.say(line)
                done.append((plan, source.offline_for(plan.spec.key)))
            except (OSError, ValueError) as exc:
                self.say(f"{plan.spec.key}: FAILED -- {exc}")
        return done

    def _download(self, keys) -> None:
        jobs = []
        for k in keys:
            kind = self._kind(k)
            dl = self._entry(k)["downloads"][kind]
            jobs.append((k, asset_url(self.catalog, dl["file"]), dl))
        self._dl_tmp = tempfile.TemporaryDirectory(prefix="aaltoflow_dl_")
        tmp = Path(self._dl_tmp.name)
        self._busy_net, self._cancel = True, False
        self._sync_buttons()

        def work():
            got, errors = [], []
            for key, url, dl in jobs:
                self._dl_note.emit(f"downloading {dl['file']} ({dl['size'] / 1e6:.1f} MB)…")
                last = [-1]

                def progress(done, total, last=last):
                    if total:
                        pct = int(100 * done / total) // 25 * 25
                        if pct != last[0] and pct < 100:
                            last[0] = pct
                            self._dl_note.emit(f"   {pct} %")
                try:
                    got.append((key, download(url, tmp / dl["file"], dl["sha256"],
                                              dl["size"], progress,
                                              cancelled=lambda: self._cancel)))
                    self._dl_note.emit(f"   verified (SHA-256)")
                except CatalogError as exc:
                    errors.append(f"{key}: {exc}")
                    if self._cancel:
                        break
            self._dl_done.emit(got, errors)
        threading.Thread(target=work, daemon=True).start()

    def _on_downloaded(self, got, errors) -> None:
        self._busy_net = False
        for e in errors:
            self.say(e)
        done = []
        for key, path in got:
            try:
                with ModuleSource(path) as src:
                    done += self._install_from(src, [key])
            except (ValueError, OSError) as exc:
                self.say(f"{key}: FAILED -- {exc}")
        if self._dl_tmp is not None:
            self._dl_tmp.cleanup()
            self._dl_tmp = None
        if self.catalog is not None:
            self._on_fetched(self.catalog, "")   # re-plan against what is now here
        self._after_copy(done)

    def _after_copy(self, done: list) -> None:
        """Rescan, then build the environments of what was copied."""
        build = getattr(self, "_build", True)
        self._sync_buttons()
        self.installed.emit()
        if not build or not done:
            if done:
                self.say("copied. Build the environment later: Start menu > Rebuild "
                         "Python environments (or uv sync in the module folder).")
            return
        uv = find_uv()
        if uv is None:
            self.say("uv not found -- cannot build the environment now.")
            return
        self._queue, self._failed = [], []
        for p, offline in done:
            try:
                self._queue += env_steps(p.target, self.root, uv, offline)
            except (FileNotFoundError, ManifestError) as exc:
                self.say(f"{p.spec.key}: {exc}")
        self._next_step()

    def _next_step(self) -> None:
        if not self._queue:
            self._proc = None
            self.say("environments: " + ("FAILED for " + ", ".join(self._failed)
                                         if self._failed else "all built."))
            self._sync_buttons()
            self.installed.emit()                # the card can now start its service
            return
        step = self._queue.pop(0)
        self.say(f"> {step.label}")
        proc = QtCore.QProcess(self)
        proc.setWorkingDirectory(str(step.cwd))
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.remove("UV_PROJECT_ENVIRONMENT")    # would make every project share one env
        env.insert("NO_COLOR", "1")
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda pr=proc: self.say(bytes(pr.readAllStandardOutput()).decode(
                "utf-8", "replace").rstrip()))
        proc.finished.connect(lambda code, _st, st=step: self._step_done(st, code))
        self._proc = proc
        self._sync_buttons()
        proc.start(step.argv[0], step.argv[1:])

    def _step_done(self, step, code: int) -> None:
        if code != 0:
            key = step.label.split(":")[0]
            self._failed.append(key)
            self.say(f"{step.label}: exit code {code}")
            self._queue = [s for s in self._queue if not s.label.startswith(key + ":")]
        self._next_step()

    def closeEvent(self, ev):
        if self._proc is not None:
            ev.ignore()                          # never abandon a half-built .venv
            return
        self._cancel = True                      # a running download stops at its next chunk
        if self.source is not None:
            self.source.close()
        super().closeEvent(ev)


# ──────────────────────────────── module card ─────────────────────────────

class ModuleCard(QtWidgets.QFrame):
    """One module: identity, live status, actions, and its variables."""

    def __init__(self, spec: ModuleSpec, win: "MainWindow"):
        super().__init__()
        self.spec = spec
        self.win = win
        self.up = False
        self.service_proc: QtCore.QProcess | None = None
        self.gui_procs: list[QtCore.QProcess] = []
        self._stopping = False        # True while WE are killing the service
        self.manifest: dict | None = None

        self.setObjectName("card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10); outer.setSpacing(6)
        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        outer.addLayout(row)

        self.icon = QtWidgets.QLabel(); self.icon.setFixedWidth(44)
        row.addWidget(self.icon)

        namebox = QtWidgets.QVBoxLayout(); namebox.setSpacing(1)
        self.name = QtWidgets.QLabel(); self.name.setObjectName("name")
        self.desc = QtWidgets.QLabel(); self.desc.setObjectName("meta")
        self.meta = QtWidgets.QLabel(); self.meta.setObjectName("meta")
        namebox.addWidget(self.name); namebox.addWidget(self.desc); namebox.addWidget(self.meta)
        row.addLayout(namebox, 1)

        self.real_check = QtWidgets.QCheckBox("real")
        self.real_check.setToolTip("On: start the service with --real (drives the instrument).\n"
                                   "Off: simulated backend. Remembered on this PC.")
        self.real_check.clicked.connect(self._real_clicked)     # user clicks only
        row.addWidget(self.real_check)

        self.status_dot = QtWidgets.QLabel()
        self.status_txt = QtWidgets.QLabel(); self.status_txt.setObjectName("meta")
        self.status_txt.setFixedWidth(84)
        row.addWidget(self.status_dot); row.addWidget(self.status_txt)

        st = self.style()
        self.btn_service = QtWidgets.QPushButton("  Service"); self.btn_service.setObjectName("primary")
        self.btn_service.setIcon(st.standardIcon(QtWidgets.QStyle.SP_MediaPlay))
        self.btn_service.clicked.connect(self.start_service)
        self.btn_stop = QtWidgets.QPushButton("  Stop"); self.btn_stop.setObjectName("danger")
        self.btn_stop.setIcon(st.standardIcon(QtWidgets.QStyle.SP_MediaStop))
        self.btn_stop.clicked.connect(self.stop_service)
        self.btn_gui = QtWidgets.QPushButton("  GUI")
        self.btn_gui.setIcon(st.standardIcon(QtWidgets.QStyle.SP_ComputerIcon))
        self.btn_gui.clicked.connect(self.open_gui)
        for b in (self.btn_service, self.btn_stop, self.btn_gui):
            b.setFixedWidth(92)
            row.addWidget(b)

        self.btn_ports = QtWidgets.QPushButton("Ports")
        self.btn_ports.setToolTip("Change this module's ports on this PC")
        self.btn_ports.clicked.connect(self.edit_ports)
        self.btn_remove = QtWidgets.QPushButton("Remove"); self.btn_remove.setObjectName("danger")
        self.btn_remove.setToolTip("Delete this remote service from the list "
                                   "(the service on the other PC is not touched)")
        self.btn_remove.clicked.connect(self.remove)
        self.btn_vars = QtWidgets.QPushButton("Variables")
        self.btn_vars.setCheckable(True)
        self.btn_vars.toggled.connect(self._toggle_vars)
        for b in (self.btn_ports, self.btn_remove):
            b.setFixedWidth(84)
            row.addWidget(b)
        self.btn_vars.setFixedWidth(112)          # "Variables 35" must fit
        row.addWidget(self.btn_vars)

        # the variables, from `describe`
        self.vars = QtWidgets.QTreeWidget()
        self.vars.setHeaderLabels(["variable", "unit", "range / type", "id"])
        self.vars.setRootIsDecorated(True)
        self.vars.setMinimumHeight(160); self.vars.setMaximumHeight(260)
        self.vars_note = QtWidgets.QLabel(); self.vars_note.setObjectName("meta")
        self.vars.hide(); self.vars_note.hide()
        outer.addWidget(self.vars_note)
        outer.addWidget(self.vars)

        self.set_spec(spec)
        manifest, seen = load_cached_describe(spec.id)
        if manifest:
            self.set_variables(manifest, cached_at=seen)
        self.set_up(False)

    # ---- identity ---------------------------------------------------------

    @property
    def owns_service(self) -> bool:
        return self.service_proc is not None and self.service_proc.state() != QtCore.QProcess.NotRunning

    def set_spec(self, spec: ModuleSpec):
        """(Re)apply a discovered spec: ports, flags and labels may have changed."""
        self.spec = spec
        self.icon.setPixmap(module_icon(spec))
        self.name.setText(spec.name + ("   [remote]" if spec.remote else ""))
        self.desc.setText(spec.description or "")
        where = f"{spec.host}" if spec.remote else (spec.dir.name if spec.dir else "")
        ports = f"cmd {spec.cmd} · pub {spec.pub}"
        if spec.ports_overridden:
            ports += f"  (changed; default {spec.default_cmd}/{spec.default_pub})"
        extra = "" if spec.has_gui else ("  ·  no GUI (headless)" if spec.dir else
                                         "  ·  not installed on this PC (no GUI)")
        self.meta.setText(f"{where}   ·   {ports}{extra}")
        self.real_check.setVisible(not spec.remote)
        self.real_check.setChecked(spec.real)
        for b in (self.btn_service, self.btn_stop, self.btn_ports):
            b.setVisible(not spec.remote)
        self.btn_remove.setVisible(spec.remote)
        self.btn_gui.setEnabled(spec.has_gui)
        self.btn_gui.setToolTip("" if spec.has_gui else "No GUI for this module on this PC.")
        self.set_up(self.up)

    # ---- launching --------------------------------------------------------

    def _spawn(self, script: str, extra: list[str], gui: bool, label: str) -> QtCore.QProcess:
        project_dir = self.spec.dir
        prog, args = build_command(project_dir, script, extra, gui, prefer_venv=not gui)
        proc = QtCore.QProcess(self)
        proc.setWorkingDirectory(str(project_dir))
        proc.setProgram(prog)
        proc.setArguments(args)
        # Every module learns where every other module listens (the camera
        # needs kim's port, which may have been changed on this PC).
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert(ENDPOINTS_ENV, endpoints_json(self.win.found.modules))
        # Python buffers print() when stdout is a pipe, so a service's messages
        # would reach the log only when it exits. Unbuffered = they arrive live.
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc, l=label: self._pipe(p, l))
        proc.errorOccurred.connect(lambda err, l=label, pr=prog: self._on_proc_error(err, l, pr))
        self.win.log(f"[{self.spec.id}] {label}: {prog} {' '.join(args)}  (cwd={project_dir.name})")
        proc.start()
        return proc

    def _on_proc_error(self, err, label: str, prog: str):
        # Only a genuine start failure is worth a red line. "Crashed" is what
        # QProcess reports when WE kill it on Stop.
        if err == QtCore.QProcess.ProcessError.FailedToStart:
            hint = ("uv not found (PATH or %USERPROFILE%\\.local\\bin)" if prog == "uv"
                    else "has this project been synced (uv sync --extra gui)?")
            self.win.log(f"[{self.spec.id}] {label}: failed to start '{prog}'. {hint}", "error")

    def _pipe(self, proc: QtCore.QProcess, label: str):
        text = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        for line in text.splitlines():
            if line.strip():
                self.win.log(f"[{self.spec.id}·{label}] {line.rstrip()}")

    def start_service(self):
        if not self.spec.can_start:
            return
        if probe(self.spec.host, self.spec.cmd):
            self.win.log(f"[{self.spec.id}] something already listens on {self.spec.cmd} "
                         f"-- not starting a second one.", "warn")
            return
        self._stopping = False
        self.win.log(f"[{self.spec.id}] starting in {'REAL' if self.spec.real else 'SIM'} mode "
                     f"on {self.spec.cmd}/{self.spec.pub}")
        self.service_proc = self._spawn(self.spec.service, service_args(self.spec),
                                        gui=False, label="service")
        self.service_proc.finished.connect(self._service_finished)
        self.set_up(self.up)
        self.win.prober.probe_now()

    def _service_finished(self, code, _status):
        if self._stopping:
            self.win.log(f"[{self.spec.id}] service stopped.")
        elif code not in (0, None):
            self.win.log(f"[{self.spec.id}] service exited unexpectedly (code {code}).", "warn")
        else:
            self.win.log(f"[{self.spec.id}] service exited (code {code}).")
        self._stopping = False
        self.service_proc = None
        self.set_up(self.up)
        self.win.prober.probe_now()

    def stop_service(self, graceful_wait_ms: int = 8000):
        proc = self.service_proc
        if proc and proc.state() != QtCore.QProcess.NotRunning:
            self._stopping = True
            pid = int(proc.processId() or 0)
            self.win.log(f"[{self.spec.id}] stopping service…")
            # First ask it to stop itself, so it can close its hardware.
            if request_shutdown(self.spec.host, self.spec.cmd) and \
                    proc.waitForFinished(graceful_wait_ms):
                self.win.prober.probe_now()
                return
            proc.terminate()
            if not proc.waitForFinished(2000):
                proc.kill()
            # Backstop: via `uv run` the real python is a CHILD of what we killed.
            if sys.platform == "win32" and pid:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.win.prober.probe_now()
        elif not self.spec.remote:
            self.win.log(f"[{self.spec.id}] no launcher-owned service to stop "
                         f"(if it is up, it was started elsewhere -- close it in its own window).", "warn")

    def open_gui(self):
        if not self.spec.has_gui:
            return
        if self.spec.remote and not self.up:
            self.win.log(f"[{self.spec.id}] {self.spec.host}:{self.spec.cmd} is not reachable; "
                         f"not opening a GUI (it would silently run a local simulator).", "warn")
            return
        mode = "connected to the live service" if self.up else "standalone simulator"
        self.win.log(f"[{self.spec.id}] opening GUI ({mode})…")
        self.gui_procs.append(self._spawn(self.spec.gui, gui_args(self.spec, connect=self.up),
                                          gui=True, label="gui"))

    # ---- settings ---------------------------------------------------------

    def _real_clicked(self, on: bool):
        set_real(self.spec.key, on, ROOT)
        self.spec.real = on
        self.win.sync_all_box()

    def edit_ports(self):
        if self.up:
            QtWidgets.QMessageBox.information(
                self, "Ports", "Stop the service first: a running service keeps the ports "
                               "it was started with.")
            return
        others = [m for m in self.win.found.modules if m.id != self.spec.id]
        dlg = PortsDialog(self.spec, others, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.result_ports is not None:
            cmd, pub = dlg.result_ports
            set_ports(self.spec.key, cmd, pub, ROOT)
            self.win.log(f"[{self.spec.id}] ports "
                         + ("reset to default" if cmd is None else f"set to {cmd}/{pub}"))
            self.win.rescan(force=True)

    def remove(self):
        ok = QtWidgets.QMessageBox.question(
            self, "Remove remote service",
            f"Remove '{self.spec.name}' ({self.spec.host}:{self.spec.cmd}) from the list?\n\n"
            f"The service on the other PC keeps running; this only forgets it here.")
        if ok == QtWidgets.QMessageBox.Yes:
            remove_remote(self.spec.id, ROOT)
            self.win.log(f"removed remote {self.spec.id}")
            self.win.rescan(force=True)

    # ---- status + variables -----------------------------------------------

    def set_up(self, up: bool):
        was = self.up
        self.up = up
        if up and self.owns_service:
            self.status_dot.setPixmap(dot(C["ok"])); self.status_txt.setText("running")
        elif up:
            self.status_dot.setPixmap(dot(C["accent"]))
            self.status_txt.setText("reachable" if self.spec.remote else "up (external)")
        elif self.owns_service:
            self.status_dot.setPixmap(dot(C["accent_dim"])); self.status_txt.setText("starting…")
        else:
            self.status_dot.setPixmap(dot(C["muted"])); self.status_txt.setText("down")
        self.btn_service.setEnabled(self.spec.can_start and not up and not self.owns_service)
        self.btn_stop.setEnabled(self.owns_service)
        if up and not was:
            # just came up: ask what it can do (a restarted service may have changed)
            self.win.prober.describe(self.spec.id, self.spec.host, self.spec.cmd)

    def set_variables(self, manifest: dict | None, cached_at: float | None = None):
        if not manifest:
            return
        self.manifest = manifest
        params = manifest.get("parameters", [])
        groups = {"control": [], "indicator": [], "action": []}
        for p in params:
            groups.setdefault(p.get("kind", "indicator"), []).append(p)
        self.vars.clear()
        for kind, title in (("control", "Controls"), ("indicator", "Measured"),
                            ("action", "Actions")):
            items = groups.get(kind) or []
            if not items:
                continue
            top = QtWidgets.QTreeWidgetItem([f"{title} ({len(items)})"])
            self.vars.addTopLevelItem(top)
            for p in sorted(items, key=lambda d: (d.get("group", ""), d.get("order", 0))):
                if kind == "control" and ("min" in p or "max" in p):
                    rng = f"{p.get('min', '')} … {p.get('max', '')}"
                elif p.get("options"):
                    rng = " / ".join(map(str, p["options"]))
                else:
                    rng = p.get("type", "")
                top.addChild(QtWidgets.QTreeWidgetItem(
                    [p.get("label", p.get("id", "")), p.get("unit", ""), str(rng), p.get("id", "")]))
            top.setExpanded(kind != "action")
        for col in range(4):
            self.vars.resizeColumnToContents(col)
        n_c, n_m = len(groups["control"]), len(groups["indicator"])
        self.btn_vars.setText(f"Variables {n_c + n_m}")
        if cached_at:
            self.vars_note.setText("last seen " + time.strftime("%Y-%m-%d %H:%M", time.localtime(cached_at))
                                   + " (service not asked since)")
        else:
            self.vars_note.setText("live from the running service")

    def _toggle_vars(self, on: bool):
        if on and self.manifest is None:
            self.vars_note.setText("No variables yet: start the service once and they appear "
                                   "(and are remembered afterwards).")
        self.vars.setVisible(on and self.manifest is not None)
        self.vars_note.setVisible(on)

    def set_active(self, on: bool):
        """Briefly highlight this card (used when a profile is activated)."""
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


# ────────────────────────────── main window ───────────────────────────────

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        # "TR-MOKE · Mission Control" when the installer was told which setup
        # this PC drives, "AaltoFlow · Mission Control" otherwise
        self.setWindowTitle(suite_title("Mission Control"))
        self.resize(1180, 900)
        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        col = QtWidgets.QVBoxLayout(root)
        col.setContentsMargins(18, 16, 18, 16); col.setSpacing(12)

        self.bridge = Bridge()
        self.bridge.probed.connect(self._on_probed)
        self.bridge.described.connect(self._on_described)
        self.prober = Prober(self.bridge)
        self.suite_proc: QtCore.QProcess | None = None   # scan-core, opened at most once
        self.viewer_proc: QtCore.QProcess | None = None  # the data viewer, likewise

        # header
        header = QtWidgets.QHBoxLayout()
        tbox = QtWidgets.QVBoxLayout(); tbox.setSpacing(2)
        title = QtWidgets.QLabel(suite_title("Mission Control").upper()); title.setObjectName("title")
        title.setToolTip("The setup name comes from the installer (re-run Setup to change it,\n"
                         "or edit settings.setup_name in suite_local.json).")
        # the product name moves to the subtitle once a setup name leads the title
        lead = f"{PRODUCT} · " if setup_name() else ""
        sub = QtWidgets.QLabel(lead + "finds the modules · starts services · opens GUIs")
        sub.setObjectName("subtitle")
        tbox.addWidget(title); tbox.addWidget(sub)
        header.addLayout(tbox); header.addStretch(1)
        self.real_check = QtWidgets.QCheckBox("Real hardware: all")
        self.real_check.setToolTip("Sets every local module's 'real' box at once "
                                   f"(remembered in {LOCAL_FILE}).")
        self.real_check.clicked.connect(self._set_all_real)
        header.addWidget(self.real_check, 0, QtCore.Qt.AlignVCenter)
        col.addLayout(header)

        # global actions
        bar = QtWidgets.QHBoxLayout()
        start_all = QtWidgets.QPushButton("  Start all services"); start_all.setObjectName("primary")
        start_all.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay))
        start_all.clicked.connect(self.start_all)
        guis_all = QtWidgets.QPushButton("  Open all GUIs")
        guis_all.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon))
        guis_all.clicked.connect(self.open_all_guis)
        self.suite_btn = QtWidgets.QPushButton("  Measurement suite")
        self.suite_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
        self.suite_btn.setToolTip("scan-core: define and run scans. It connects to the "
                                  "services running here (Settings tab: follow the launcher).")
        self.suite_btn.clicked.connect(self.open_suite)
        self.viewer_btn = QtWidgets.QPushButton("  Data viewer")
        self.viewer_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirOpenIcon))
        self.viewer_btn.setToolTip("scan-core: look at saved measurements -- maps, 1-D "
                                   "overlays, export to files, Origin and Jupyter.")
        self.viewer_btn.clicked.connect(self.open_viewer)
        stop_all = QtWidgets.QPushButton("  Stop all"); stop_all.setObjectName("danger")
        stop_all.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaStop))
        stop_all.clicked.connect(self.stop_all)
        bar.addWidget(start_all); bar.addWidget(guis_all); bar.addWidget(self.suite_btn)
        bar.addWidget(self.viewer_btn)
        bar.addStretch(1); bar.addWidget(stop_all)
        col.addLayout(bar)

        # profiles
        self.profiles = load_profiles()
        prow = QtWidgets.QHBoxLayout()
        ptag = QtWidgets.QLabel("PROFILES"); ptag.setObjectName("sectiontag"); ptag.setFixedWidth(84)
        prow.addWidget(ptag)
        self.profile_bar = QtWidgets.QHBoxLayout(); self.profile_bar.setSpacing(8)
        prow.addLayout(self.profile_bar)
        prow.addStretch(1)
        self.exclusive_check = QtWidgets.QCheckBox("Exclusive")
        self.exclusive_check.setToolTip("When on, activating a profile first STOPS every "
                                        "launcher-owned service that isn't in the profile.")
        prow.addWidget(self.exclusive_check, 0, QtCore.Qt.AlignVCenter)
        edit_btn = QtWidgets.QPushButton("Edit…"); edit_btn.setFixedWidth(70)
        edit_btn.clicked.connect(self.edit_profiles)
        prow.addWidget(edit_btn)
        col.addLayout(prow)

        # modules
        mrow = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel("MODULES"); tag.setObjectName("sectiontag")
        mrow.addWidget(tag)
        self.found_lbl = QtWidgets.QLabel(); self.found_lbl.setObjectName("meta")
        mrow.addWidget(self.found_lbl)
        mrow.addStretch(1)
        rescan = QtWidgets.QPushButton("Rescan")
        rescan.setToolTip(f"Look for module folders again (also happens by itself "
                          f"when a {MANIFEST} or {LOCAL_FILE} changes)")
        rescan.clicked.connect(lambda: self.rescan(force=True))
        add_mod = QtWidgets.QPushButton("Add module…")
        add_mod.setToolTip("Install modules on this PC from a folder or a module pack (.zip)")
        add_mod.clicked.connect(self.add_module)
        add = QtWidgets.QPushButton("Add remote…")
        add.setToolTip("Add a service that runs on another PC")
        add.clicked.connect(self.add_remote)
        mrow.addWidget(rescan); mrow.addWidget(add_mod); mrow.addWidget(add)
        col.addLayout(mrow)

        self.problems_lbl = QtWidgets.QLabel(); self.problems_lbl.setWordWrap(True)
        self.problems_lbl.setStyleSheet(f"color:{C['danger']};")
        self.problems_lbl.hide()
        col.addWidget(self.problems_lbl)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget(); holder.setObjectName("root")
        self.vlist = QtWidgets.QVBoxLayout(holder)
        self.vlist.setContentsMargins(0, 0, 0, 0); self.vlist.setSpacing(10)
        self.vlist.addStretch(1)
        scroll.setWidget(holder)
        col.addWidget(scroll, 1)

        # log
        logtag = QtWidgets.QHBoxLayout()
        lt = QtWidgets.QLabel("LOG"); lt.setObjectName("sectiontag")
        clear = QtWidgets.QPushButton("Clear"); clear.setFixedWidth(80)
        clear.clicked.connect(lambda: self.logbox.clear())
        logtag.addWidget(lt); logtag.addStretch(1); logtag.addWidget(clear)
        col.addLayout(logtag)
        self.logbox = QtWidgets.QPlainTextEdit(); self.logbox.setObjectName("log")
        self.logbox.setReadOnly(True); self.logbox.setFixedHeight(160)
        self.logbox.setMaximumBlockCount(1000)
        col.addWidget(self.logbox)

        self.cards: dict[str, ModuleCard] = {}
        self._signature = None
        self.log(f"root = {ROOT}")
        migrate_launcher_json(self.log)
        self.rescan(force=True)
        real = [m.id for m in self.found.modules if m.real]
        self.log(f"real hardware: {', '.join(real) if real else 'none (all simulated)'}")
        self.log(f"uv: {find_uv() or 'NOT FOUND'}")

        self.rescan_timer = QtCore.QTimer(self)
        self.rescan_timer.timeout.connect(lambda: self.rescan(force=False))
        self.rescan_timer.start(RESCAN_PERIOD_MS)

    # ---- discovery --------------------------------------------------------

    def _folder_signature(self):
        """Cheap fingerprint of everything discovery depends on."""
        parts = []
        for path in sorted(ROOT.glob(f"*/{MANIFEST}")):
            try:
                parts.append((str(path), path.stat().st_mtime_ns))
            except OSError:
                pass
        try:
            parts.append((LOCAL_FILE, (ROOT / LOCAL_FILE).stat().st_mtime_ns))
        except OSError:
            parts.append((LOCAL_FILE, 0))
        return tuple(parts)

    def rescan(self, force: bool = False):
        """Re-read the modules. Keeps existing cards (they may own running
        processes) and only adds, updates and removes what changed."""
        sig = self._folder_signature()
        if not force and sig == self._signature:
            return
        first = self._signature is None
        self._signature = sig
        self.found = discover(ROOT)
        ids = [m.id for m in self.found.modules]

        for mid in list(self.cards):
            if mid not in ids:
                card = self.cards[mid]
                if card.owns_service:
                    self.log(f"[{mid}] its {MANIFEST} is gone, but its service is still "
                             f"running from here -- keeping the card until it stops.", "warn")
                    continue
                self.vlist.removeWidget(card)
                card.deleteLater()
                del self.cards[mid]
                if not first:
                    self.log(f"module gone: {mid}")

        for i, spec in enumerate(self.found.modules):
            card = self.cards.get(spec.id)
            if card is None:
                card = ModuleCard(spec, self)
                self.cards[spec.id] = card
                if not first:
                    self.log(f"module found: {spec.id} ({spec.name})")
            else:
                card.set_spec(spec)
            self.vlist.removeWidget(card)
            self.vlist.insertWidget(i, card)

        self.prober.set_targets([(m.id, m.host, m.cmd) for m in self.found.modules])
        self.prober.probe_now()

        n_local = sum(not m.remote for m in self.found.modules)
        n_remote = len(self.found.modules) - n_local
        self.found_lbl.setText(f"   {n_local} local" + (f" · {n_remote} remote" if n_remote else ""))
        if self.found.problems:
            self.problems_lbl.setText("⚠  " + "\n⚠  ".join(self.found.problems))
            self.problems_lbl.show()
            if not first:
                for p in self.found.problems:
                    self.log(p, "warn")
        else:
            self.problems_lbl.hide()
        if first:
            for p in self.found.problems:
                self.log(p, "warn")
        self.build_profile_bar()
        self.sync_all_box()

    def _on_probed(self, result: dict):
        for mid, up in result.items():
            card = self.cards.get(mid)
            if card is not None:
                card.set_up(up)

    def _on_described(self, mid: str, manifest):
        card = self.cards.get(mid)
        if card is None or not manifest:
            return
        if manifest.get("module") not in (None, card.spec.key):
            self.log(f"[{mid}] the service on {card.spec.host}:{card.spec.cmd} says it is "
                     f"'{manifest.get('module')}', not '{card.spec.key}' -- wrong port?", "warn")
            return
        save_cached_describe(mid, manifest)
        card.set_variables(manifest)

    def add_module(self) -> "AddModuleDialog":
        """Non-modal: an environment build takes minutes, the launcher stays usable."""
        dlg = AddModuleDialog(ROOT, self)
        dlg.installed.connect(lambda: self.rescan(force=True))
        dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dlg.show()
        return dlg

    def add_remote(self):
        dlg = AddRemoteDialog([m for m in self.found.modules if not m.remote], self)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.added_id:
            self.log(f"added remote {dlg.added_id}")
            self.rescan(force=True)

    # ---- helpers ----------------------------------------------------------

    def local_cards(self) -> list[ModuleCard]:
        return [self.cards[m.id] for m in self.found.modules
                if not m.remote and m.id in self.cards]

    def _set_all_real(self, on: bool):
        for card in self.local_cards():
            set_real(card.spec.key, on, ROOT)
            card.spec.real = on
            card.real_check.setChecked(on)
        self.sync_all_box()

    def sync_all_box(self):
        cards = self.local_cards()
        self.real_check.blockSignals(True)
        self.real_check.setChecked(bool(cards) and all(c.spec.real for c in cards))
        self.real_check.blockSignals(False)

    def log(self, msg: str, level: str = "info"):
        color = {"error": C["danger"], "warn": C["accent_hi"]}.get(level, C["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.logbox.appendHtml(
            f'<span style="color:{C["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    # ---- profiles ---------------------------------------------------------

    def build_profile_bar(self):
        while self.profile_bar.count():
            w = self.profile_bar.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        full = dict(name=FULL_SUITE, members=[m.id for m in self.found.modules if not m.remote])
        for prof in self.profiles + [full]:
            present = [self.cards[k].spec.name for k in prof["members"] if k in self.cards]
            missing = [k for k in prof["members"] if k not in self.cards]
            btn = QtWidgets.QPushButton(prof["name"])
            btn.setObjectName("profile")
            tip = f"Bring up: {', '.join(present) or '(nothing available)'}"
            if missing:
                tip += f"\nNot found on this PC: {', '.join(missing)}"
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, p=prof: self.activate_profile(p))
            self.profile_bar.addWidget(btn)

    def activate_profile(self, profile: dict, open_guis: bool = True):
        members = [self.cards[k] for k in profile["members"] if k in self.cards]
        if not members:
            self.log(f"profile '{profile['name']}' has no modules available here.", "warn")
            return
        self.log(f"profile '{profile['name']}' → {', '.join(c.spec.name for c in members)}")
        if self.exclusive_check.isChecked():
            ids = {c.spec.id for c in members}
            for c in self.cards.values():
                if c.spec.id not in ids and c.owns_service:
                    self.log(f"exclusive: stopping {c.spec.name}")
                    c.stop_service()
        for c in members:
            c.set_active(True)
        QtCore.QTimer.singleShot(1600, lambda cs=list(members): [c.set_active(False) for c in cs])
        self._start_cards([c for c in members if c.spec.can_start])
        if open_guis:
            n_local = sum(c.spec.can_start for c in members)
            base = n_local * 500 + 800           # let services bind before GUIs connect
            for j, c in enumerate([c for c in members if c.spec.has_gui]):
                QtCore.QTimer.singleShot(base + j * 400, c.open_gui)

    def _start_cards(self, cards: list[ModuleCard]):
        """Start services in dependency order (start_after), staggered."""
        by_id = {c.spec.id: c for c in cards}
        ordered = start_order([c.spec for c in cards])
        for i, spec in enumerate(ordered):
            QtCore.QTimer.singleShot(i * 500, by_id[spec.id].start_service)

    def edit_profiles(self):
        dlg = ProfileEditor(self.profiles, self.found.modules, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.profiles = dlg.result_profiles()
            try:
                save_profiles(self.profiles)
                self.log(f"saved {len(self.profiles)} profile(s) to {PROFILES_FILE.name}.")
            except OSError as e:
                self.log(f"could not save profiles.json: {e}", "error")
            self.build_profile_bar()

    # ---- global actions ---------------------------------------------------

    def start_all(self):
        self.log("starting all local services…")
        self._start_cards([c for c in self.local_cards() if c.spec.can_start])

    def open_all_guis(self):
        self.log("opening all GUIs…")
        for i, card in enumerate([c for c in self.cards.values() if c.spec.has_gui]):
            QtCore.QTimer.singleShot(i * 400, card.open_gui)

    def open_suite(self):
        """Open scan-core's measurement suite (one instance).

        No ZeroMQ of our own: the suite discovers the same modules we do and
        connects to the ones that are running.
        """
        self.suite_proc = self._open_app(SUITE_SCRIPT, "suite", "measurement suite",
                                         self.suite_proc)

    def open_viewer(self):
        """Open the data viewer (one instance). It needs no running service."""
        self.viewer_proc = self._open_app(VIEWER_SCRIPT, "viewer", "data viewer",
                                          self.viewer_proc)

    def _open_app(self, script: str, tag: str, what: str,
                  running: QtCore.QProcess | None) -> QtCore.QProcess | None:
        """Start one of scan-core's applications, its output piped into the log
        as [tag]. Returns the process to keep (the running one if already open)."""
        project = ROOT / SUITE_DIR
        if not (project / script).is_file():
            self.log(f"no {what} at {project / script}", "error")
            return running
        if running is not None and running.state() != QtCore.QProcess.NotRunning:
            self.log(f"the {what} is already open.")
            return running
        prog, args = build_command(project, script, [], gui=True, prefer_venv=False)
        proc = QtCore.QProcess(self)
        proc.setWorkingDirectory(str(project))
        proc.setProgram(prog)
        proc.setArguments(args)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert(ENDPOINTS_ENV, endpoints_json(self.found.modules))
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc, t=tag: self._pipe_app(p, t))
        proc.errorOccurred.connect(
            lambda err, pr=prog, t=tag: self.log(f"[{t}] failed to start '{pr}'", "error")
            if err == QtCore.QProcess.ProcessError.FailedToStart else None)
        self.log(f"[{tag}] {prog} {' '.join(args)}  (cwd={project.name})")
        proc.start()
        return proc

    def _pipe_app(self, proc, tag: str):
        text = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        for line in text.splitlines():
            if line.strip():
                self.log(f"[{tag}] {line.rstrip()}")

    def stop_all(self):
        self.log("stopping all launcher-owned services…")
        for card in self.cards.values():
            if card.owns_service:
                card.stop_service()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.rescan_timer.stop()
        self.prober.stop()
        # Stop owned services PROPERLY: closing the window used to call
        # terminate() and move on, and the QProcess objects then died with the
        # window -- a hard kill, which can leave hardware (the PM16) stuck.
        for card in self.cards.values():
            if card.owns_service:
                card.stop_service()
        super().closeEvent(ev)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AaltoFlow Mission Control")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="UI theme for this launch (default: %s)" % DEFAULT_THEME)
    args = ap.parse_args(argv)

    set_theme(args.theme or DEFAULT_THEME)      # BEFORE any widget is built
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
