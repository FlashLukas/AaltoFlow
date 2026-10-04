"""Mission Control's "Security..." window: the encryption chain without typing.

What it manages (README, "Encryption and keys"):
  * THIS PC's key pair -- the public half is a padlock that may be shared,
    the secret half never leaves the PC;
  * the lab KEYRING -- a folder with one key file per trusted PC, plus the
    lab's policy.json; whoever can write into it is trusted;
  * the POLICY -- off / warn / enforce, and which modules it covers. It is
    LAB-WIDE: every PC that uses the keyring reads the same file.

Every step is done by suite_common/keyadmin.py, which tools/keys.py uses
too, so the window and the command line cannot disagree. This file only
shows what keyadmin returns and turns its errors into a readable line at the
bottom of the window (never a traceback): a share that is offline, or a
keyring this PC may not write, is a normal situation, not a crash.

Three tabs:
  This PC      -- key yes/no, keyring, "in the keyring", machine, policy, and
                  one plain line of what to do next;
  Trusted PCs  -- the keyring as a table: may-run-scans checkbox, add a PC
                  from its key file, retire a PC (and restore a retired one);
  Lab policy   -- the mode and the modules, applied after a confirmation,
                  then which services on THIS PC must restart.

Colours come from theme.COLORS at the moment something is shown (never a
constant here), so the window follows the light and the dark theme.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from suite_common import keyadmin as KA
from suite_common import secure

from theme import COLORS as C

#: one plain sentence per mode (the README says the same)
MODE_TEXT = {
    "off": "Plain, exactly as before: nothing is encrypted and nobody is checked.",
    "warn": "Everything is encrypted. PCs that are not in the keyring, and false "
            "identities, are let through but written to the service's log. Switch "
            "this on first, and look for warnings for a week.",
    "enforce": "Everything is encrypted. PCs that are not in the keyring get no answer, "
               "and false identities are refused.",
}

#: the badge colour of each mode (a theme key, read when the badge is drawn)
MODE_COLOR = {"off": "muted", "warn": "accent", "enforce": "ok"}


def badge_text(pol: dict) -> str:
    """'Security: off' / 'Security: warn (all modules)' / 'Security: enforce (3 modules)'."""
    return "Security: " + KA.describe_policy(pol)


def style_badge(label: QtWidgets.QLabel, pol: dict) -> None:
    """Text and colour of the main window's badge, from the active theme."""
    label.setText(badge_text(pol))
    color = C[MODE_COLOR.get(pol.get("mode", "off"), "muted")]
    label.setStyleSheet(f"color:{color}; font-weight:700; border:1px solid {color};"
                        f"border-radius:8px; padding:3px 8px;")


def _value(text: str, color_key: str | None = None) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    if color_key:
        lbl.setStyleSheet(f"color:{C[color_key]};")
    return lbl


class AddPcDialog(QtWidgets.QDialog):
    """Name and may-run-scans for a PC whose key file is being added
    (prefilled from what the file says about itself)."""

    def __init__(self, kf: KA.KeyFile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a PC to the keyring")
        lay = QtWidgets.QFormLayout(self)
        lay.addRow(_value(f"Key file: {kf.path.name}   (key {kf.public[:8]}...)"))
        self.name = QtWidgets.QLineEdit(kf.pc)
        self.name.setPlaceholderText("the PC's name, e.g. lab-pc-1")
        lay.addRow("PC name", self.name)
        self.machine = QtWidgets.QCheckBox("this PC may run scans (machine)")
        self.machine.setChecked(kf.machine)
        self.machine.setToolTip("Programs on it may act as 'machine': run scans, drive "
                                "another module (they pass the control lock).")
        lay.addRow(self.machine)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok |
                                        QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Add")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


class SecurityDialog(QtWidgets.QDialog):
    """The Security window (non-modal: it stays open next to the cards)."""

    COLS = ("PC", "May run scans", "Key", "Addresses", "")

    def __init__(self, parent=None, modules=None, restart=None, log=None, on_change=None):
        """modules(): the module keys Mission Control found (for the policy
        checklist). restart(keys): stop + start those services on this PC.
        log(msg, level): Mission Control's log. on_change(): the policy or
        keyring changed (the main window refreshes its badge)."""
        super().__init__(parent)
        self.modules = modules or (lambda: [])
        self.restart = restart
        self.log = log or (lambda msg, level="info": None)
        self.on_change = on_change or (lambda: None)
        self.stale: list[dict] = []
        self.setWindowTitle("Security -- keys, keyring and the lab policy")
        self.resize(900, 600)
        lay = QtWidgets.QVBoxLayout(self)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_pc_tab(), "This PC")
        self.tabs.addTab(self._build_ring_tab(), "Trusted PCs")
        self.tabs.addTab(self._build_policy_tab(), "Lab policy")
        lay.addWidget(self.tabs, 1)

        bottom = QtWidgets.QHBoxLayout()
        # every result and every error lands here, in words (never a traceback)
        self.msg = QtWidgets.QLabel()
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        bottom.addWidget(self.msg, 1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(close, 0, QtCore.Qt.AlignBottom)
        lay.addLayout(bottom)
        self.refresh()

    # ------------------------------------------------------------ plumbing --

    def say(self, text: str, level: str = "info") -> None:
        """Show a result or an error at the bottom, and put it in the log."""
        color = {"error": C["danger"], "warn": C["accent_hi"], "ok": C["ok"]}.get(
            level, C["muted"])
        self.msg.setStyleSheet(f"color:{color};")
        self.msg.setText(text)
        self.log(f"[security] {text}", "info" if level == "ok" else level)

    def _do(self, what: str, fn, *args, **kwargs):
        """Run one keyadmin step; any failure becomes a readable line."""
        try:
            return fn(*args, **kwargs)
        except KA.AdminError as exc:
            self.say(f"{what}: {exc}", "error")
        except OSError as exc:
            self.say(f"{what}: {exc.strerror or exc} ({getattr(exc, 'filename', '') or ''})",
                     "error")
        except Exception as exc:          # never let a click end in a traceback
            self.say(f"{what} failed: {type(exc).__name__}: {exc}", "error")
        return None

    # the questions the window asks; tests replace these
    def confirm(self, title: str, text: str) -> bool:
        box = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return box == QtWidgets.QMessageBox.Yes

    def pick_dir(self, start: str = "") -> str:
        return QtWidgets.QFileDialog.getExistingDirectory(self, "The lab keyring folder", start)

    def pick_open(self) -> str:
        return QtWidgets.QFileDialog.getOpenFileName(
            self, "A PC's public key file", "", "Key files (*.key);;All files (*)")[0]

    def pick_save(self, name: str) -> str:
        return QtWidgets.QFileDialog.getSaveFileName(
            self, "Save this PC's public key", name, "Key files (*.key)")[0]

    def ask_add(self, kf: KA.KeyFile):
        """(name, machine) for a key file being added, or None (cancelled)."""
        dlg = AddPcDialog(kf, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dlg.name.text().strip(), dlg.machine.isChecked()

    def refresh(self) -> None:
        """Re-read everything (status, keyring, policy). A keyring read is
        usually fast; it is done on open, after every change and on Refresh,
        never on a timer."""
        self._fill_pc()
        self._fill_ring()
        self._fill_policy()

    # -------------------------------------------------------------- This PC --

    def _build_pc_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        intro = QtWidgets.QLabel(
            "Every PC has a key pair: the public half is like a padlock (one small file, "
            "may be shared), the secret half never leaves the PC. The lab's keyring is a "
            "folder with the public key of every trusted PC and the lab's policy.")
        intro.setWordWrap(True)
        intro.setObjectName("meta")
        lay.addWidget(intro)

        self.form = QtWidgets.QFormLayout()
        self.form.setLabelAlignment(QtCore.Qt.AlignRight)
        self.v_pc, self.v_key, self.v_ring = _value(""), _value(""), _value("")
        self.v_in, self.v_machine, self.v_policy = _value(""), _value(""), _value("")
        for label, v in (("This PC", self.v_pc), ("Its key", self.v_key),
                         ("Keyring folder", self.v_ring), ("In the keyring", self.v_in),
                         ("May run scans (machine)", self.v_machine),
                         ("Lab policy", self.v_policy)):
            self.form.addRow(label, v)
        lay.addLayout(self.form)

        self.advice = QtWidgets.QLabel()
        self.advice.setWordWrap(True)
        self.advice.setObjectName("advice")
        lay.addWidget(self.advice)

        row = QtWidgets.QHBoxLayout()
        self.btn_choose = QtWidgets.QPushButton("Choose keyring folder...")
        self.btn_choose.setToolTip("Point this PC at the lab's keyring (a folder, usually "
                                   "on a network share). If it is empty, it can become a "
                                   "new keyring.")
        self.btn_choose.clicked.connect(self.choose_keyring)
        self.btn_make = QtWidgets.QPushButton("Make this PC's key")
        self.btn_make.setObjectName("primary")
        self.btn_make.clicked.connect(self.make_key)
        self.machine_box = QtWidgets.QCheckBox("this PC may run scans (machine)")
        self.machine_box.setToolTip("Programs on this PC may act as 'machine' on other PCs "
                                    "(scan-core, the camera driving kim).")
        self.btn_export = QtWidgets.QPushButton("Save public key to file...")
        self.btn_export.setToolTip("For bringing it to the lab PC on a USB stick or the "
                                   "share. Only the public half: safe to share.")
        self.btn_export.clicked.connect(self.export_key)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_choose)
        row.addWidget(self.btn_make)
        row.addWidget(self.machine_box)
        row.addStretch(1)
        row.addWidget(self.btn_export)
        row.addWidget(refresh)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _fill_pc(self) -> None:
        st = self._do("reading this PC's security", KA.status)
        if st is None:
            return
        self.status = st
        self.v_pc.setText(st.pc)
        if st.has_key:
            name = f" (as '{st.key_pc}')" if st.key_pc != st.pc else ""
            self.v_key.setText(f"yes, {st.public_prefix}...{name}")
            self.v_key.setStyleSheet(f"color:{C['ok']};")
        else:
            self.v_key.setText("none yet")
            self.v_key.setStyleSheet(f"color:{C['danger']};")
        if st.keyring is None:
            self.v_ring.setText("none chosen")
            self.v_ring.setStyleSheet(f"color:{C['danger']};")
        else:
            reach = "" if st.keyring_reachable else "   (NOT reachable from this PC)"
            self.v_ring.setText(f"{st.keyring}{reach}")
            self.v_ring.setStyleSheet(f"color:{C['text' if st.keyring_reachable else 'danger']};")
        if st.in_keyring is None:
            self.v_in.setText("cannot tell yet")
            self.v_in.setStyleSheet(f"color:{C['muted']};")
        elif st.in_keyring:
            self.v_in.setText(f"yes, as '{st.keyring_name}' ({st.keyring_file})")
            self.v_in.setStyleSheet(f"color:{C['ok']};")
        else:
            self.v_in.setText("NO -- other PCs will not let this one in")
            self.v_in.setStyleSheet(f"color:{C['danger']};")
        self.v_machine.setText("--" if st.machine is None else ("yes" if st.machine else "no"))
        self.v_machine.setStyleSheet(f"color:{C['text']};")
        mods = st.policy["modules"]
        mods = "all" if "*" in mods else (", ".join(mods) or "none")
        self.v_policy.setText(f"{st.policy['mode']}  (secured modules: {mods})")
        self.v_policy.setStyleSheet(f"color:{C[MODE_COLOR.get(st.policy['mode'], 'muted')]};")
        advice = st.advice()
        ok = advice[0].startswith("All set")
        self.advice.setText("\n".join(("" if ok else "! ") + a for a in advice))
        self.advice.setStyleSheet(f"color:{C['ok' if ok else 'accent_hi']}; font-weight:600;"
                                  f"padding:8px; border:1px solid {C['border']};"
                                  f"border-radius:8px; background:{C['panel']};")
        self.btn_make.setText("Make a new key for this PC" if st.has_key
                              else "Make this PC's key")
        self.btn_make.setObjectName("" if st.has_key else "primary")
        self.btn_make.style().unpolish(self.btn_make)
        self.btn_make.style().polish(self.btn_make)
        self.btn_export.setEnabled(st.has_key)
        if st.machine is not None:          # a new key keeps what the keyring says now
            self.machine_box.setChecked(st.machine)

    def choose_keyring(self) -> None:
        start = str(self.status.keyring) if getattr(self, "status", None) and \
            self.status.keyring else ""
        folder = self.pick_dir(start)
        if not folder:
            return
        warnings = self._do("choosing the keyring", KA.use_keyring, folder)
        if warnings is None:
            return
        try:
            has_policy = (Path(folder) / secure.POLICY_FILE).is_file()
        except OSError:
            has_policy = False
        if not has_policy and self.confirm(
                "A new lab keyring?",
                f"{folder} has no policy.json, so it is not a lab keyring yet.\n\n"
                "Make it the lab's keyring? Security stays OFF (nothing changes) until "
                "you choose a mode in the Lab policy tab.\n\n"
                "Only the lab's administrator should be able to write to this folder: "
                "whoever can put a file into it is trusted."):
            if self._do("creating the keyring", KA.init_keyring, folder, "off", ["*"]) is None:
                return
            self.say(f"new lab keyring: {folder} (policy 'off'). Next: make this PC's key.",
                     "ok")
        elif warnings:
            self.say(f"this PC uses the keyring {folder} -- " + "; ".join(warnings), "warn")
        else:
            self.say(f"this PC uses the keyring {folder}", "ok")
        self.refresh()
        self.on_change()

    def make_key(self) -> None:
        force = False
        if getattr(self, "status", None) and self.status.has_key:
            if not self.confirm(
                    "Replace this PC's key?",
                    "This PC already has a key. A NEW key replaces it, and the old one stops "
                    "working everywhere at once: until the new public key is in the keyring, "
                    "the other PCs' secured modules will not let this PC in.\n\n"
                    "Make a new key?"):
                return
            force = True
        made = self._do("making the key", KA.make_key, machine=self.machine_box.isChecked(),
                        force=force, here=secure.security_dir())
        if made is None:
            return
        if made.keyring_file is not None:
            self.say(f"this PC's key is made, as '{made.pc}', and is in the keyring "
                     f"({made.keyring_file.name}).", "ok")
        else:
            self.say(f"this PC's key is made, as '{made.pc}'. The keyring is not writable "
                     f"from here: use 'Save public key to file...' and add that file on a PC "
                     f"that may write the keyring (Trusted PCs > Add a PC from its key "
                     f"file...).", "warn")
        self.refresh()

    def export_key(self) -> None:
        name = f"{getattr(self.status, 'key_pc', '') or secure.this_pc_name()}.key"
        path = self.pick_save(name)
        if not path:
            return
        out = self._do("saving the public key", KA.export_public, path)
        if out is not None:
            self.say(f"public key saved to {out} -- safe to share. On the lab PC: Security... "
                     f"> Trusted PCs > Add a PC from its key file...", "ok")

    # ---------------------------------------------------------- Trusted PCs --

    def _build_ring_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        intro = QtWidgets.QLabel(
            "The PCs whose programs may reach the secured modules. 'May run scans' lets "
            "a PC's programs act as a machine (scan-core, the camera driving kim). A "
            "change works within a few seconds on every PC.")
        intro.setWordWrap(True)
        intro.setObjectName("meta")
        lay.addWidget(intro)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_ring_buttons)
        lay.addWidget(self.table, 1)
        self.ring_note = QtWidgets.QLabel()
        self.ring_note.setWordWrap(True)
        lay.addWidget(self.ring_note)

        row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Add a PC from its key file...")
        self.btn_add.setObjectName("primary")
        self.btn_add.setToolTip("A public key file brought from that PC ('Save public key to "
                                "file...' there). It is written fresh into the keyring from "
                                "this PC, so every PC can read it.")
        self.btn_add.clicked.connect(self.add_pc)
        self.btn_retire = QtWidgets.QPushButton("Retire PC...")
        self.btn_retire.setObjectName("danger")
        self.btn_retire.setToolTip("Stop trusting the selected PC. Its key file is kept in the "
                                   "keyring's retired/ folder, so it can be restored.")
        self.btn_retire.clicked.connect(self.retire_pc)
        self.btn_restore = QtWidgets.QPushButton("Restore")
        self.btn_restore.setToolTip("Trust a retired PC again")
        self.btn_restore.clicked.connect(self.restore_pc)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_retire)
        row.addWidget(self.btn_restore)
        row.addStretch(1)
        row.addWidget(refresh)
        lay.addLayout(row)
        return w

    def _cell(self, text: str, color_key: str | None = None) -> QtWidgets.QTableWidgetItem:
        it = QtWidgets.QTableWidgetItem(text)
        if color_key:
            from PySide6 import QtGui
            it.setForeground(QtGui.QBrush(QtGui.QColor(C[color_key])))
        return it

    def _fill_ring(self) -> None:
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.rows: list[tuple[str, str]] = []           # (kind, name) per table row
        kr = secure.keyring_dir()
        try:
            entries, problems = KA.entries()
            gone = KA.retired()
        except KA.AdminError as exc:
            self.ring_note.setText(str(exc))
            self.ring_note.setStyleSheet(f"color:{C['danger']};")
            self.btn_add.setEnabled(False)
            self._sync_ring_buttons()
            return
        except OSError as exc:
            self.ring_note.setText(f"the keyring cannot be read: {exc.strerror or exc}")
            self.ring_note.setStyleSheet(f"color:{C['danger']};")
            self.btn_add.setEnabled(False)
            self._sync_ring_buttons()
            return
        self.btn_add.setEnabled(True)
        own = KA.own_public()
        for e in entries:
            r = self._add_row("pc", e.pc)
            self.table.setItem(r, 0, self._cell(e.pc))
            box = QtWidgets.QCheckBox()
            box.setChecked(e.machine)
            box.setToolTip(f"may programs on {e.pc} act as a machine (run scans)?")
            box.clicked.connect(lambda on, pc=e.pc, b=box: self.set_machine(pc, on, b))
            holder = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(holder)
            hl.setContentsMargins(8, 0, 8, 0)
            hl.addWidget(box)
            hl.addStretch(1)
            self.table.setCellWidget(r, 1, holder)
            self.table.setItem(r, 2, self._cell(e.public[:8] + "...", "muted"))
            self.table.setItem(r, 3, self._cell(" ".join(e.addresses), "muted"))
            self.table.setItem(r, 4, self._cell("this PC" if e.public == own else "", "accent"))
        for p in problems:
            fname = p.split(":", 1)[0]
            r = self._add_row("problem", fname)
            self.table.setItem(r, 0, self._cell(Path(fname).stem, "danger"))
            self.table.setItem(r, 1, self._cell(
                f"{p} -- add it again from this PC (Add a PC from its key file...)", "danger"))
            self.table.setSpan(r, 1, 1, len(self.COLS) - 1)
        for g in gone:
            r = self._add_row("retired", g.file.name)
            self.table.setItem(r, 0, self._cell(g.pc, "muted"))
            self.table.setItem(r, 1, self._cell(
                f"retired {g.date or ''} -- not trusted (Restore lets it in again)", "muted"))
            self.table.setSpan(r, 1, 1, len(self.COLS) - 1)
        self.table.resizeRowsToContents()
        n = len(entries)
        self.ring_note.setText(f"{n} trusted PC(s) in {kr}" +
                               (f"; {len(problems)} key file(s) cannot be read here"
                                if problems else "") +
                               (f"; {len(gone)} retired" if gone else ""))
        self.ring_note.setStyleSheet(f"color:{C['danger' if problems else 'muted']};")
        self._sync_ring_buttons()

    def _add_row(self, kind: str, name: str) -> int:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.rows.append((kind, name))
        return r

    def selected_row(self) -> tuple[str, str] | None:
        sel = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not sel:
            return None
        r = sel[0].row()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _sync_ring_buttons(self) -> None:
        sel = self.selected_row()
        self.btn_retire.setEnabled(sel is not None and sel[0] in ("pc", "problem"))
        self.btn_restore.setEnabled(sel is not None and sel[0] == "retired")

    def select_pc(self, name: str) -> bool:
        """Select the row of `name` (a PC, or a file name); True if found."""
        for r, (_, n) in enumerate(self.rows):
            if n == name or Path(n).stem == name or \
                    (self.table.item(r, 0) and self.table.item(r, 0).text() == name):
                self.table.selectRow(r)
                return True
        return False

    def set_machine(self, pc: str, on: bool, box: QtWidgets.QCheckBox | None = None) -> None:
        done = self._do(f"changing {pc}", KA.set_machine, pc, on)
        if done is None:
            if box is not None:                   # put the box back: nothing changed
                box.blockSignals(True)
                box.setChecked(not on)
                box.blockSignals(False)
            return
        self.say(f"{pc}: may run scans (machine) = {'yes' if on else 'no'}", "ok")
        self._fill_pc()

    def add_pc(self) -> None:
        path = self.pick_open()
        if not path:
            return
        kf = self._do("reading the key file", KA.peek_key_file, path)
        if kf is None:
            return
        if kf.has_secret:
            self.say(f"{Path(path).name} holds a SECRET key: it must never leave its PC and "
                     f"is never added. On that PC use 'Save public key to file...'.", "error")
            return
        answer = self.ask_add(kf)
        if not answer:
            return
        name, machine = answer
        try:
            added = KA.add_from_file(path, machine=machine, pc_name=name)
        except KA.NeedsOverwrite:
            if not self.confirm("Replace a PC's key?",
                                f"'{name}' is already in the keyring. Replace its key with "
                                f"the one in {Path(path).name}?\n\nThe key it has now stops "
                                f"working at once."):
                return
            added = self._do("adding the PC", KA.add_from_file, path, machine=machine,
                             pc_name=name, overwrite=True)
        except (KA.AdminError, OSError) as exc:
            self.say(f"adding the PC: {exc}", "error")
            return
        if added is None:
            return
        self.say(f"{'replaced' if added.replaced else 'added'} '{added.pc}' "
                 f"({added.file.name}); may run scans = {'yes' if machine else 'no'}", "ok")
        self.refresh()
        self.on_change()

    def retire_pc(self) -> None:
        sel = self.selected_row()
        if sel is None or sel[0] not in ("pc", "problem"):
            self.say("select the PC to retire first", "warn")
            return
        name = sel[1] if sel[0] == "pc" else Path(sel[1]).stem
        text = (f"{name} will no longer reach any secured module, from now on -- also the "
                f"connections it has open now, within a few seconds.\n\n"
                f"Its key file is kept in the keyring's retired/ folder: Restore undoes this.")
        force = False
        mine = self._is_this_pc(name)
        if mine:
            text = (f"'{name}' is THIS PC. Retiring it locks this PC out of every secured "
                    f"module on the other PCs (its own services still answer it).\n\n" + text)
            force = True
        if not self.confirm(f"Retire {name}?", text):
            return
        r = self._do(f"retiring {name}", KA.retire, name, force=force)
        if r is None:
            return
        self.say(f"retired {r.pc}: it no longer reaches any secured module (kept in "
                 f"retired/{r.file.name})", "ok")
        self.refresh()
        self.on_change()

    def _is_this_pc(self, name: str) -> bool:
        st = getattr(self, "status", None)
        names = {secure.this_pc_name()}
        if st is not None:
            names |= {st.key_pc, st.keyring_name} - {""}
        return name.lower() in names

    def restore_pc(self) -> None:
        sel = self.selected_row()
        if sel is None or sel[0] != "retired":
            self.say("select a retired PC first", "warn")
            return
        path = self._do("restoring", KA.restore, sel[1])
        if path is None:
            return
        self.say(f"restored {path.stem}: it is trusted again", "ok")
        self.refresh()
        self.on_change()

    # ----------------------------------------------------------- Lab policy --

    def _build_policy_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(w)
        left = QtWidgets.QVBoxLayout()
        head = QtWidgets.QLabel("MODE  (the whole lab: every PC that uses this keyring)")
        head.setObjectName("sectiontag")
        left.addWidget(head)
        self.mode_buttons: dict[str, QtWidgets.QRadioButton] = {}
        self.mode_group = QtWidgets.QButtonGroup(w)
        for mode in secure.MODES:
            rb = QtWidgets.QRadioButton(mode)
            rb.setStyleSheet("font-weight:700;")
            self.mode_group.addButton(rb)
            self.mode_buttons[mode] = rb
            left.addWidget(rb)
            expl = QtWidgets.QLabel(MODE_TEXT[mode])
            expl.setWordWrap(True)
            expl.setObjectName("meta")
            expl.setContentsMargins(24, 0, 0, 6)
            left.addWidget(expl)
        left.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply to the whole lab...")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self.apply_policy)
        left.addWidget(self.apply_btn)
        self.stale_lbl = QtWidgets.QLabel()
        self.stale_lbl.setWordWrap(True)
        left.addWidget(self.stale_lbl)
        srow = QtWidgets.QHBoxLayout()
        self.restart_btn = QtWidgets.QPushButton("Restart them")
        self.restart_btn.setToolTip("Stop (cleanly) and start again the services listed above "
                                    "that this Mission Control started")
        self.restart_btn.clicked.connect(self.restart_stale)
        srow.addWidget(self.restart_btn)
        srow.addStretch(1)
        left.addLayout(srow)
        outer.addLayout(left, 3)

        right = QtWidgets.QVBoxLayout()
        head2 = QtWidgets.QLabel("MODULES IT COVERS")
        head2.setObjectName("sectiontag")
        right.addWidget(head2)
        self.all_mods = QtWidgets.QRadioButton("All modules")
        self.some_mods = QtWidgets.QRadioButton("Only these:")
        grp = QtWidgets.QButtonGroup(w)
        grp.addButton(self.all_mods)
        grp.addButton(self.some_mods)
        # The list stays ENABLED (a disabled list hides its check boxes in the
        # dark theme): "All modules" ticks every box, and ticking or unticking
        # one by hand means "Only these".
        self.all_mods.toggled.connect(self._all_toggled)
        right.addWidget(self.all_mods)
        right.addWidget(self.some_mods)
        self.mod_list = QtWidgets.QListWidget()
        self.mod_list.itemChanged.connect(self._item_changed)
        right.addWidget(self.mod_list, 1)
        note = QtWidgets.QLabel("A module that is not covered keeps talking plain, and every "
                                "client talks plain to it.")
        note.setWordWrap(True)
        note.setObjectName("meta")
        right.addWidget(note)
        outer.addLayout(right, 2)
        self._show_stale([])
        return w

    def _fill_policy(self) -> None:
        pol = KA.get_policy()
        self.mode_buttons.get(pol["mode"], self.mode_buttons["off"]).setChecked(True)
        mods = pol["modules"]
        keys = sorted({str(k).lower() for k in self.modules()} | {m for m in mods if m != "*"})
        self.mod_list.blockSignals(True)
        self.mod_list.clear()
        for k in keys:
            it = QtWidgets.QListWidgetItem(k)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if k in mods else QtCore.Qt.Unchecked)
            self.mod_list.addItem(it)
        self.mod_list.blockSignals(False)
        everything = "*" in mods or not keys
        self.all_mods.setChecked(everything)
        self.some_mods.setChecked(not everything)
        if everything:
            self._all_toggled(True)
        kr = secure.keyring_dir()
        self.apply_btn.setEnabled(kr is not None)
        self.apply_btn.setToolTip("" if kr is not None else "choose the keyring folder first "
                                  "(This PC tab)")

    def _all_toggled(self, on: bool) -> None:
        if not on:
            return
        self.mod_list.blockSignals(True)
        for i in range(self.mod_list.count()):
            self.mod_list.item(i).setCheckState(QtCore.Qt.Checked)
        self.mod_list.blockSignals(False)

    def _item_changed(self, _item) -> None:
        if self.all_mods.isChecked():
            self.some_mods.setChecked(True)

    def chosen_policy(self) -> dict:
        mode = next((m for m, rb in self.mode_buttons.items() if rb.isChecked()), "off")
        if self.all_mods.isChecked():
            mods = ["*"]
        else:
            mods = [self.mod_list.item(i).text() for i in range(self.mod_list.count())
                    if self.mod_list.item(i).checkState() == QtCore.Qt.Checked]
        return {"mode": mode, "modules": mods}

    def set_choice(self, mode: str, modules) -> None:
        """Set the tab's controls (tests and scripts)."""
        self.mode_buttons[mode].setChecked(True)
        if "*" in modules:
            self.all_mods.setChecked(True)
        else:
            self.some_mods.setChecked(True)
            self.mod_list.blockSignals(True)
            for i in range(self.mod_list.count()):
                it = self.mod_list.item(i)
                it.setCheckState(QtCore.Qt.Checked if it.text() in modules
                                 else QtCore.Qt.Unchecked)
            self.mod_list.blockSignals(False)

    def confirm_text(self, new: dict) -> str:
        """What 'Apply' will do, in words -- with the extra checks for enforce."""
        lines = [f"This changes the security policy of the WHOLE lab -- every PC that uses "
                 f"this keyring -- to: {KA.describe_policy(new)}.",
                 "", MODE_TEXT[new["mode"]]]
        if new["mode"] == "enforce":
            try:
                entries, problems = KA.entries()
            except KA.AdminError:
                entries, problems = [], []
            names = ", ".join(e.pc for e in entries) or "NONE"
            machines = ", ".join(e.pc for e in entries if e.machine) or "none"
            lines += ["", "Before enforce, check:",
                      f"  - PCs in the keyring: {names}",
                      f"  - of them, may run scans: {machines}",
                      "  - every PC NOT listed gets no answer from the secured modules"]
            if problems:
                lines.append(f"  - {len(problems)} key file(s) cannot be read from this PC: "
                             f"those PCs may be locked out (Trusted PCs tab)")
            lines.append("  - the keyring folder must be writable ONLY by the lab's "
                         "administrator: whoever can put a file into it is trusted")
        lines += ["", "Services that are running keep their old mode until they restart "
                      "(this window lists the ones on this PC). Apply?"]
        return "\n".join(lines)

    def apply_policy(self) -> None:
        new = self.chosen_policy()
        if new["mode"] != "off" and not new["modules"]:
            self.say("choose at least one module (or All modules)", "warn")
            return
        if not self.confirm("Change the lab's security policy?", self.confirm_text(new)):
            return
        done = self._do("changing the policy", KA.set_policy, new["mode"], new["modules"])
        if done is None:
            return
        self.say(f"lab policy is now {KA.describe_policy(done)}", "ok")
        stale = self._do("looking for services to restart", KA.stale_services, done) or []
        self._show_stale(stale)
        self.refresh()
        self.on_change()

    def _show_stale(self, stale: list[dict]) -> None:
        self.stale = stale
        if not stale:
            self.stale_lbl.setText("")
            self.restart_btn.hide()
            return
        names = ", ".join(f"{r.get('module')} (running {r.get('mode')})" for r in stale)
        self.stale_lbl.setText(
            f"Still running in their old mode on this PC -- restart them: {names}.\n"
            "Services on OTHER PCs need a restart there too (the policy is lab-wide).")
        self.stale_lbl.setStyleSheet(f"color:{C['accent_hi']};")
        self.restart_btn.setVisible(self.restart is not None)

    def restart_stale(self) -> None:
        if not self.stale or self.restart is None:
            return
        keys = sorted({str(r.get("module", "")).lower() for r in self.stale})
        self.restart(keys)
        self.say(f"restarting {', '.join(keys)} (see the log)", "ok")
        self._show_stale([])
