"""recall.py -- set instruments back to the settings stored in a data file.

Lukas (2026-10-04): "a way how to set it back - an options list listing
differences between current and saved set and options to select what to
recall."

Every scan file carries a snapshot of every connected instrument
(scan_core/snapshot.py). This dialog puts it next to the instruments as they
are NOW:

* one branch per instrument in the file; under it, each SETTING (get_config,
  "group.key") whose value now differs from the file: value now | value in
  file. "Show all settings" lists the equal ones too.
* nothing is ticked by default -- recalling is a deliberate act, setting by
  setting (or "Select all differences" of one instrument).
* the instrument's STATE at scan time (status: positions, readings) is shown
  for information and cannot be ticked: only settings can be sent back.
* an instrument in the file that is not connected now, or whose module is not
  the same one (another module under the same name), is greyed out.
* "Apply selected" lists what will change, asks, then sends ONE set_config per
  instrument with ONLY the ticked keys (gotcha #5: never the whole config). It
  goes as a PERSON (the suite's gui identity): if another PC holds control of
  that instrument, it is refused, and the dialog says so -- it never forces.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from scan_core import run_info
from scan_core.snapshot import (MISSING, all_settings, diff_config,
                                partial_config, path_text, read_snapshot,
                                recallable, same_value, value_text)
from apps.theme import C

#: Shown in the dialog: what a recall can and cannot promise.
HELP_TEXT = (
    "Settings are sent back as plain values. Ones that depend on a calibration "
    "or on the hardware's state -- a camera's spot calibration, kim's step "
    "sizes, a stage's zero -- may not mean the same thing now: check the "
    "instrument after recalling.")

ROLE_KEY = QtCore.Qt.UserRole          # (slug, path) on a setting row


def _person(inst):
    """Send as a PERSON ("gui"), like the Control tab: a GUI at another PC
    that holds control must not be overridden (suite_common/control.py)."""
    return getattr(inst, "gui_command", None) or inst.command


def _module_of(inst) -> str:
    manifest = getattr(inst, "manifest", None) or {}
    return manifest.get("module") or getattr(inst, "name", "")


def _find_instrument(lab, slug: str):
    """The live connection whose registry prefix is `slug`, or None."""
    if lab is None:
        return None
    from scan_core.lab import module_prefix
    for name, inst in getattr(lab, "instruments", {}).items():
        try:
            if module_prefix(inst) == slug:
                return inst
        except Exception:
            if name == slug:
                return inst
    return None


class RecallDialog(QtWidgets.QDialog):
    """See the module docstring. `snapshot` = read_snapshot(...); `attrs` =
    the file's attributes (for the header); `lab` = the suite's Lab or None."""

    def __init__(self, snapshot: dict, lab=None, attrs: dict | None = None,
                 file_label: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recall instrument settings")
        self.resize(900, 640)
        self.snapshot = snapshot
        self.lab = lab
        self.attrs = attrs or {}
        #: per slug: {"inst", "state", "note", "current"} (see _probe)
        self.live: dict = {}
        #: (slug, path) ticked by the user; survives "show all" and a refresh
        self.ticked: set = set()
        #: per slug: (ok, text) of the last Apply
        self.results: dict = {}
        #: replaced by tests: (title, text) -> bool
        self.confirm = self._ask

        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(8)
        v.addWidget(self._header(file_label))

        help_lbl = QtWidgets.QLabel(HELP_TEXT)
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        v.addWidget(help_lbl)

        opts = QtWidgets.QHBoxLayout()
        self.show_all = QtWidgets.QCheckBox("Show all settings (not only differences)")
        self.show_all.toggled.connect(lambda *_: self.rebuild())
        opts.addWidget(self.show_all)
        opts.addStretch(1)
        self.select_btn = QtWidgets.QPushButton("Select all differences of this instrument")
        self.select_btn.setToolTip("Ticks every recallable difference of the instrument\n"
                                   "the selected row belongs to.")
        self.select_btn.clicked.connect(self.select_current_instrument)
        opts.addWidget(self.select_btn)
        clear = QtWidgets.QPushButton("Clear ticks")
        clear.clicked.connect(self.clear_ticks)
        opts.addWidget(clear)
        v.addLayout(opts)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["setting", "now", "in file"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 250)
        self.tree.itemChanged.connect(self._item_changed)
        v.addWidget(self.tree, 1)

        self.report = QtWidgets.QLabel("")
        self.report.setWordWrap(True)
        self.report.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.report)

        btns = QtWidgets.QHBoxLayout()
        refresh = QtWidgets.QPushButton("Re-read instruments")
        refresh.clicked.connect(self.refresh)
        btns.addWidget(refresh)
        btns.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply selected")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self.apply_selected)
        btns.addWidget(self.apply_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        v.addLayout(btns)

        self.refresh()

    # ---- header ----------------------------------------------------------

    def _header(self, file_label: str) -> QtWidgets.QWidget:
        """The file, and WHAT it was (run info), read-only."""
        box = QtWidgets.QFrame(); box.setObjectName("card")
        g = QtWidgets.QGridLayout(box)
        g.setContentsMargins(12, 8, 12, 8); g.setHorizontalSpacing(14); g.setVerticalSpacing(2)
        a = self.attrs
        title = QtWidgets.QLabel(file_label or a.get("name", "") or "data file")
        title.setStyleSheet("font-weight:700;")
        g.addWidget(title, 0, 0, 1, 4)
        rows = [("scan", a.get("name", "")), ("created", a.get("created", "")),
                ("snapshot", a.get("snapshot_time", "")),
                ("setup", a.get("setup_name", ""))]
        rows += [(f, a.get(f, "")) for f in run_info.FIELDS]
        self.header_values = {}
        shown = [(k, str(v)) for k, v in rows if str(v or "").strip()]
        for i, (k, val) in enumerate(shown):
            r, c = divmod(i, 2)
            key = QtWidgets.QLabel(k); key.setStyleSheet(f"color:{C['muted']};")
            lbl = QtWidgets.QLabel(val); lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(key, 1 + r, 2 * c); g.addWidget(lbl, 1 + r, 2 * c + 1)
            self.header_values[k] = val
        g.setColumnStretch(1, 1); g.setColumnStretch(3, 1)
        return box

    # ---- live side -------------------------------------------------------

    def _probe(self, slug: str, entry: dict) -> dict:
        """Is this instrument here, the same module, and what is it set to?"""
        inst = _find_instrument(self.lab, slug)
        if inst is None:
            return {"inst": None, "state": "absent", "note": "not connected",
                    "current": None}
        saved_module = entry.get("module", "")
        if saved_module and _module_of(inst) != saved_module:
            return {"inst": None, "state": "other",
                    "note": f"different module now ({_module_of(inst)}, file: "
                            f"{saved_module}) -- not recallable",
                    "current": None}
        try:
            cfg = inst.command("get_config", _timeout_ms=2000).get("config") or {}
        except Exception as exc:
            return {"inst": inst, "state": "error",
                    "note": f"could not read its settings: {exc}", "current": None}
        try:
            status = inst.latest() if hasattr(inst, "latest") else None
        except Exception:
            status = None
        return {"inst": inst, "state": "ok", "note": "connected",
                "current": cfg, "status": status or {}}

    def refresh(self) -> None:
        """Re-read every instrument's settings and rebuild the list."""
        self.live = {slug: self._probe(slug, entry or {})
                     for slug, entry in self.snapshot.items()}
        self.rebuild()

    # ---- the tree --------------------------------------------------------

    def rebuild(self) -> None:
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            grey = QtGui.QBrush(QtGui.QColor(C["muted"]))
            for slug in sorted(self.snapshot):
                entry = self.snapshot[slug] or {}
                live = self.live.get(slug, {})
                saved = entry.get("config") or {}
                top = QtWidgets.QTreeWidgetItem(self.tree)
                rev = entry.get("revision")
                head = f"{slug}  ({entry.get('module', '?')}"
                head += f", describe rev {rev})" if rev is not None else ")"
                top.setText(0, head)
                note = live.get("note", "")
                if slug in self.results:
                    ok, text = self.results[slug]
                    note = ("applied: " if ok else "REFUSED: ") + text
                top.setText(1, note)
                top.setData(0, ROLE_KEY, (slug, None))
                f = top.font(0); f.setBold(True); top.setFont(0, f)
                usable = live.get("state") == "ok"
                if not usable:
                    for col in range(3):
                        top.setForeground(col, grey)
                if entry.get("error"):
                    top.setText(2, f"snapshot incomplete: {entry['error']}")
                current = live.get("current") if usable else None
                if usable:
                    rows = (all_settings(saved, current) if self.show_all.isChecked()
                            else diff_config(saved, current))
                else:
                    # not here: still show what the file holds, greyed
                    rows = [(p, a, MISSING) for p, a, _ in all_settings(saved, {})]
                for path, a, b in rows:
                    it = QtWidgets.QTreeWidgetItem(top)
                    it.setText(0, path_text(path))
                    it.setText(1, value_text(b) if usable else "")
                    it.setText(2, value_text(a))
                    it.setToolTip(2, value_text(a, width=2000))
                    it.setToolTip(1, value_text(b, width=2000))
                    it.setData(0, ROLE_KEY, (slug, path))
                    differs = usable and (a is MISSING or b is MISSING
                                          or not same_value(a, b))
                    if usable and recallable(a, b) and differs:
                        it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                        it.setCheckState(0, QtCore.Qt.Checked if (slug, path) in self.ticked
                                         else QtCore.Qt.Unchecked)
                    else:
                        it.setFlags(it.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                        for col in range(3):
                            it.setForeground(col, grey)
                        if usable and differs:
                            it.setToolTip(0, "not recallable: the setting is missing on "
                                             "one side, or the file only kept its size")
                if usable and not rows:
                    it = QtWidgets.QTreeWidgetItem(top)
                    it.setText(0, "no differences" if not self.show_all.isChecked()
                               else "no settings")
                    it.setForeground(0, grey)
                self._add_state(top, entry, live, grey)
                top.setExpanded(usable)
        finally:
            self.tree.blockSignals(False)
        self._update_apply()

    def _add_state(self, top, entry, live, grey) -> None:
        """The instrument's STATE at scan time: information only."""
        status = entry.get("status") or {}
        if not status:
            return
        node = QtWidgets.QTreeWidgetItem(top)
        node.setText(0, "state at scan time (information only)")
        node.setForeground(0, grey)
        now = live.get("status") or {}
        for key in sorted(status):
            it = QtWidgets.QTreeWidgetItem(node)
            it.setText(0, key)
            it.setText(1, value_text(now[key]) if key in now else "")
            it.setText(2, value_text(status[key]))
            it.setFlags(it.flags() & ~QtCore.Qt.ItemIsUserCheckable)
            for col in range(3):
                it.setForeground(col, grey)
        node.setExpanded(False)

    def _item_changed(self, item, col):
        key = item.data(0, ROLE_KEY)
        if not key or key[1] is None:
            return
        if item.checkState(0) == QtCore.Qt.Checked:
            self.ticked.add(key)
        else:
            self.ticked.discard(key)
        self._update_apply()

    def _update_apply(self):
        n = len(self.ticked)
        self.apply_btn.setEnabled(n > 0)
        self.apply_btn.setText(f"Apply selected ({n})" if n else "Apply selected")

    def setting_items(self, slug: str | None = None) -> list:
        """Every setting row (for tests and the select button)."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                key = it.data(0, ROLE_KEY)
                if key and key[1] is not None and (slug is None or key[0] == slug):
                    out.append(it)
        return out

    def select_current_instrument(self) -> None:
        it = self.tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        if it is None:
            return
        slug = (it.data(0, ROLE_KEY) or (None,))[0]
        self.select_all(slug)

    def select_all(self, slug: str) -> None:
        for it in self.setting_items(slug):
            if it.flags() & QtCore.Qt.ItemIsUserCheckable:
                it.setCheckState(0, QtCore.Qt.Checked)

    def clear_ticks(self) -> None:
        self.ticked.clear()
        self.rebuild()

    # ---- apply -----------------------------------------------------------

    def plan(self) -> dict:
        """{slug: partial config} for the ticked settings."""
        by_slug: dict = {}
        for slug, path in sorted(self.ticked):
            by_slug.setdefault(slug, []).append(path)
        return {slug: partial_config(paths, (self.snapshot[slug] or {}).get("config") or {})
                for slug, paths in by_slug.items()}

    def _ask(self, title: str, text: str) -> bool:
        ans = QtWidgets.QMessageBox.question(self, title, text)
        return ans == QtWidgets.QMessageBox.Yes

    def apply_selected(self) -> dict:
        """Confirm, then one set_config per instrument. Returns self.results."""
        plan = self.plan()
        if not plan:
            return self.results
        lines = []
        for slug, paths in sorted(self._ticked_by_slug().items()):
            saved = (self.snapshot[slug] or {}).get("config") or {}
            current = self.live.get(slug, {}).get("current") or {}
            lines.append(f"{slug}:")
            flat_d = {p: (a, b) for p, a, b in all_settings(saved, current)}
            for p in paths:
                a, b = flat_d.get(p, (MISSING, MISSING))
                lines.append(f"    {path_text(p)}: {value_text(b, 30)} -> {value_text(a, 30)}")
        text = ("These settings will be sent:\n\n" + "\n".join(lines)
                + "\n\n" + HELP_TEXT + "\n\nApply?")
        if not self.confirm("Recall settings", text):
            return self.results
        for slug, partial in plan.items():
            inst = self.live.get(slug, {}).get("inst")
            if inst is None:
                self.results[slug] = (False, "not connected")
                continue
            try:
                _person(inst)("set_config", config=partial)
                n = len(self._ticked_by_slug().get(slug, []))
                self.results[slug] = (True, f"{n} setting(s) sent")
            except Exception as exc:
                msg = str(exc)
                if "read-only:" in msg:
                    msg += (" -- another PC has control of this instrument; take "
                            "control on the Control tab first (nothing was forced)")
                self.results[slug] = (False, msg)
        self.report.setText("\n".join(
            f"{slug}: " + ("applied, " if ok else "REFUSED, ") + text
            for slug, (ok, text) in sorted(self.results.items())))
        # what was applied is no longer a difference
        for slug, (ok, _) in self.results.items():
            if ok:
                self.ticked = {k for k in self.ticked if k[0] != slug}
        self.refresh()
        return self.results

    def _ticked_by_slug(self) -> dict:
        out: dict = {}
        for slug, path in sorted(self.ticked):
            out.setdefault(slug, []).append(path)
        return out


def open_recall(parent, lab, path=None, start_dir: str = "", ds=None):
    """Ask for a .nc (unless given), read its snapshot, show the dialog.

    Returns the dialog (tests), or None when nothing was opened."""
    import xarray as xr
    if ds is None:
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent, "Recall settings from a measurement", start_dir,
                "measurements (*.nc);;all files (*)")
            if not path:
                return None
        try:
            with xr.open_dataset(path) as opened:
                attrs = dict(opened.attrs)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(parent, "Recall settings",
                                          f"Could not read {path}:\n{exc}")
            return None
    else:
        attrs = dict(ds.attrs)
    snap = read_snapshot(attrs)
    if not snap:
        QtWidgets.QMessageBox.information(
            parent, "Recall settings",
            "This file has no instrument snapshot: it was measured before "
            "snapshots existed (2026-10-04), or on the simulator.")
        return None
    dlg = RecallDialog(snap, lab=lab, attrs=attrs,
                       file_label=Path(path).name if path else "", parent=parent)
    dlg.show()
    return dlg
