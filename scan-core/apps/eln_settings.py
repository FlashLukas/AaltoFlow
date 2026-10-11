"""eln_settings.py -- the Settings tab's "ELN (eLabFTW)" card (2026-10-11).

Where runs are sent by the Catalogue's "Send to eLabFTW" and, if ticked, after
every saved scan (scan_core/eln.py does the work; this is only the form).

  * the address and the options are plain preferences of THIS PC, in
    suite_local.json (eln_url, eln_attach_data, eln_auto_upload,
    eln_max_data_mb, eln_verify_tls), saved as soon as they change;
  * the API KEY is a password and is NOT kept there: "Save key" hands it to
    eln.set_api_key (Windows Credential Manager, or a keyring; a plain
    per-user file only when neither exists, and then the card says so in
    red). The field never shows a stored key back -- it only says whether one
    is stored -- and the key is never written to the log;
  * "Test connection" asks the server who the key belongs to, in a worker
    thread (an unreachable server takes the whole timeout).
"""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtWidgets

from scan_core import eln
from apps.theme import C


class _TestSignals(QtCore.QObject):
    done = QtCore.Signal(bool, str)          # ok, message


class ElnSettingsCard(QtWidgets.QFrame):
    def __init__(self, root=None, on_log=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.root = root
        self.on_log = on_log or (lambda msg: None)
        self._test_thread: threading.Thread | None = None
        self._sig = _TestSignals()
        self._sig.done.connect(self._test_done)
        s = eln.ElnSettings.load(root)

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14); v.setSpacing(8)
        tag = QtWidgets.QLabel("ELN (eLabFTW)")
        tag.setStyleSheet(f"color:{C['muted']}; font-size:10px; font-weight:700;")
        v.addWidget(tag)
        note = QtWidgets.QLabel(
            "Send runs to an eLabFTW notebook: right-click runs on the Catalogue tab > "
            "Send to eLabFTW, or tick 'after each scan' below. Each run becomes one "
            "experiment with a summary (setup, times, axes, detectors, conditions, run "
            "info), tags and a preview figure. The API key comes from eLabFTW: your user "
            "panel > API keys > a READ/WRITE key.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{C['muted']};")
        v.addWidget(note)

        form = QtWidgets.QGridLayout(); form.setHorizontalSpacing(8); form.setVerticalSpacing(6)
        form.addWidget(QtWidgets.QLabel("Address"), 0, 0)
        self.url_edit = QtWidgets.QLineEdit(s.url)
        self.url_edit.setPlaceholderText("https://eln.example.org  (the instance's start page)")
        self.url_edit.setToolTip("The address of your eLabFTW (the root, not /api/v2).\n"
                                 "Remembered on this PC (suite setting eln_url).")
        self.url_edit.editingFinished.connect(self.save)
        form.addWidget(self.url_edit, 0, 1, 1, 3)

        form.addWidget(QtWidgets.QLabel("API key"), 1, 0)
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setToolTip("Paste the key and press Save key. A stored key is never\n"
                                 "shown again; type a new one to replace it.")
        self.key_edit.returnPressed.connect(self.save_key)
        form.addWidget(self.key_edit, 1, 1)
        self.save_key_btn = QtWidgets.QPushButton("Save key")
        self.save_key_btn.clicked.connect(self.save_key)
        form.addWidget(self.save_key_btn, 1, 2)
        self.forget_btn = QtWidgets.QPushButton("Forget key")
        self.forget_btn.clicked.connect(self.forget_key)
        form.addWidget(self.forget_btn, 1, 3)
        form.setColumnStretch(1, 1)
        v.addLayout(form)

        self.store_lbl = QtWidgets.QLabel("")
        self.store_lbl.setWordWrap(True)
        v.addWidget(self.store_lbl)

        self.attach_box = QtWidgets.QCheckBox("Attach the data file (.nc) as well, up to")
        self.attach_box.setChecked(s.attach_data)
        self.attach_box.setToolTip("Off: the entry carries the summary, the preview and the path\n"
                                   "of the file on this PC. On: the .nc file is uploaded too,\n"
                                   "unless it is larger than the limit.")
        self.max_mb = QtWidgets.QDoubleSpinBox()
        self.max_mb.setRange(1, 100000); self.max_mb.setDecimals(0); self.max_mb.setSuffix(" MB")
        self.max_mb.setValue(s.max_data_mb)
        self.max_mb.setToolTip("A larger file is not attached (the entry says so).\n"
                               "Your server may have a lower upload limit of its own.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.attach_box); row.addWidget(self.max_mb); row.addStretch(1)
        v.addLayout(row)
        self.auto_box = QtWidgets.QCheckBox("Upload automatically after each scan is saved")
        self.auto_box.setChecked(s.auto_upload)
        self.auto_box.setToolTip("In the background: a failed upload is written to the log\n"
                                 "and never stops or fails the scan. Off by default.")
        v.addWidget(self.auto_box)
        self.tls_box = QtWidgets.QCheckBox("Check the server certificate (untick only for an "
                                           "in-house server with its own certificate)")
        self.tls_box.setChecked(s.verify_tls)
        v.addWidget(self.tls_box)
        for b in (self.attach_box, self.auto_box, self.tls_box):
            b.toggled.connect(lambda _on: self.save())
        self.max_mb.editingFinished.connect(self.save)

        trow = QtWidgets.QHBoxLayout()
        self.test_btn = QtWidgets.QPushButton("Test connection")
        self.test_btn.setObjectName("primary")
        self.test_btn.clicked.connect(self.test)
        trow.addWidget(self.test_btn)
        self.test_lbl = QtWidgets.QLabel("")
        self.test_lbl.setWordWrap(True)
        self.test_lbl.setStyleSheet(f"color:{C['muted']};")
        trow.addWidget(self.test_lbl, 1)
        v.addLayout(trow)
        self._show_key_state()

    # ---- settings ------------------------------------------------------------

    def settings(self) -> eln.ElnSettings:
        return eln.ElnSettings(url=self.url_edit.text().strip(),
                               attach_data=self.attach_box.isChecked(),
                               auto_upload=self.auto_box.isChecked(),
                               max_data_mb=float(self.max_mb.value()),
                               verify_tls=self.tls_box.isChecked())

    def save(self) -> None:
        try:
            self.settings().save(self.root)
        except OSError as exc:
            self.on_log(f"eLabFTW settings not remembered: {exc}")

    def _show_key_state(self) -> None:
        has = bool(eln.get_api_key())
        where = eln.key_store_name()
        self.key_edit.setPlaceholderText(
            "a key is stored - type a new one to replace it" if has
            else "paste the API key from eLabFTW")
        if eln.key_is_secure():
            self.store_lbl.setStyleSheet(f"color:{C['muted']};")
            self.store_lbl.setText(f"The key is kept in {where}, not in suite_local.json."
                                   + ("" if has else "  No key stored yet."))
        else:
            self.store_lbl.setStyleSheet(f"color:{C['danger']};")
            self.store_lbl.setText(f"Warning: no secure key store on this PC; the key goes into "
                                   f"{where}." + ("" if has else "  No key stored yet."))
        self.forget_btn.setEnabled(has)

    def save_key(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            where = eln.set_api_key(key)
        except Exception as exc:
            self.test_lbl.setStyleSheet(f"color:{C['danger']};")
            self.test_lbl.setText(f"key not stored: {exc}")
            return
        self.key_edit.clear()
        self.on_log(f"eLabFTW: API key stored in {where}")    # never the key itself
        self._show_key_state()

    def forget_key(self) -> None:
        eln.set_api_key(None)
        self.on_log("eLabFTW: API key forgotten")
        self._show_key_state()

    # ---- the connection test ---------------------------------------------

    def testing(self) -> bool:
        return self._test_thread is not None and self._test_thread.is_alive()

    def test(self) -> None:
        if self.testing():
            return
        self.save()
        typed = self.key_edit.text().strip()
        try:
            client = eln.client_from_settings(self.root, self.settings(),
                                              api_key=typed or None)
        except eln.ElabError as exc:
            self._test_done(False, str(exc))
            return
        self.test_btn.setEnabled(False)
        self.test_lbl.setStyleSheet(f"color:{C['muted']};")
        self.test_lbl.setText(f"asking {client.base_url} ...")
        sig = self._sig

        def work():
            try:
                sig.done.emit(True, eln.check_connection(client)
                              + ("  (typed key: press Save key to keep it)" if typed else ""))
            except Exception as exc:
                sig.done.emit(False, str(exc))

        self._test_thread = threading.Thread(target=work, name="eln-test", daemon=True)
        self._test_thread.start()

    def wait_test(self, timeout_s: float = 20.0) -> None:
        """Block until a test ends and its result is shown (tests)."""
        if self._test_thread is not None:
            self._test_thread.join(timeout_s)
        for _ in range(3):
            QtCore.QCoreApplication.processEvents()
            QtCore.QCoreApplication.sendPostedEvents()

    def _test_done(self, ok: bool, msg: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_lbl.setStyleSheet(f"color:{C['ok' if ok else 'danger']};")
        self.test_lbl.setText(msg)
        self.on_log(f"eLabFTW: {msg}")
