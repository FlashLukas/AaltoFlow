"""The AUX I/O tab: the DAQ's other BNCs — analog out, analog in, digital out.

All three groups are built from the channel lists in cfg.aux. Values come in
live on the status stream (aux_snapshot), and the controls call the same
aux_set_ao / aux_set_do / aux_read_ai methods whether `ctrl` is a local
Controller or a remote ClMagClient -- so this panel works identically over the
network.
"""

from __future__ import annotations

from PySide6 import QtWidgets, QtCore

from .theme import COLORS


def _card(title: str):
    frame = QtWidgets.QFrame(); frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14); lay.setSpacing(10)
    lbl = QtWidgets.QLabel(title.upper()); lbl.setObjectName("cardTitle")
    lay.addWidget(lbl)
    return frame, lay


def _short(name: str) -> str:
    return name.split("/")[-1]      # Dev1/ao0 -> ao0, Dev1/port0/line0 -> line0


class AuxPanel(QtWidgets.QWidget):
    def __init__(self, ctrl, cfg):
        super().__init__()
        self.setObjectName("root")
        self.ctrl = ctrl
        self.cfg = cfg
        self._ao_readback = {}
        self._ai_labels = {}
        self._do_boxes = {}

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._ao_card(), 1)
        col = QtWidgets.QVBoxLayout(); col.setSpacing(16)
        col.addWidget(self._ai_card())
        col.addWidget(self._do_card())
        col.addStretch(1)
        outer.addLayout(col, 1)

    # ---- analog outputs --------------------------------------------------

    def _ao_card(self):
        card, lay = _card("Analog outputs  (±10 V)")
        vmax = self.cfg.aux.v_max
        for ch in self.cfg.aux.ao_list():
            row = QtWidgets.QHBoxLayout()
            name = QtWidgets.QLabel(_short(ch)); name.setFixedWidth(48)
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(-vmax, vmax); spin.setDecimals(3); spin.setSingleStep(0.1)
            spin.setSuffix("  V")
            setb = QtWidgets.QPushButton("Set")
            setb.clicked.connect(lambda _=False, c=ch, s=spin: self.ctrl.aux_set_ao(c, s.value()))
            rb = QtWidgets.QLabel("cmd —"); rb.setStyleSheet(f"color:{COLORS['muted']};"); rb.setFixedWidth(96)
            self._ao_readback[ch] = rb
            row.addWidget(name); row.addWidget(spin, 1); row.addWidget(setb); row.addWidget(rb)
            lay.addLayout(row)
        lay.addStretch(1)
        return card

    # ---- analog inputs ---------------------------------------------------

    def _ai_card(self):
        card, lay = _card("Analog inputs  (±10 V, RSE)")
        for ch in self.cfg.aux.ai_list():
            row = QtWidgets.QHBoxLayout()
            name = QtWidgets.QLabel(_short(ch)); name.setFixedWidth(48)
            val = QtWidgets.QLabel("—  V")
            val.setStyleSheet(f"color:{COLORS['accent']}; font-weight:700; font-size:15px;")
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self._ai_labels[ch] = val
            row.addWidget(name); row.addWidget(val, 1)
            lay.addLayout(row)
        read = QtWidgets.QPushButton("Read now")
        read.clicked.connect(self._read_all_ai)
        lay.addWidget(read)
        return card

    def _read_all_ai(self):
        for ch, lbl in self._ai_labels.items():
            lbl.setText(f"{self.ctrl.aux_read_ai(ch):+.3f}  V")

    # ---- digital outputs -------------------------------------------------

    def _do_card(self):
        card, lay = _card("Digital outputs")
        for ln in self.cfg.aux.do_list():
            box = QtWidgets.QCheckBox(_short(ln))
            box.clicked.connect(lambda checked, l=ln: self.ctrl.aux_set_do(l, checked))
            self._do_boxes[ln] = box
            lay.addWidget(box)
        return card

    # ---- live update from the status stream ------------------------------

    def update_from_status(self, aux: dict | None):
        if not aux:
            return
        for ch, lbl in self._ao_readback.items():
            if ch in aux.get("ao", {}):
                lbl.setText(f"cmd {aux['ao'][ch]:+.3f} V")
        for ch, lbl in self._ai_labels.items():
            if ch in aux.get("ai", {}):
                lbl.setText(f"{aux['ai'][ch]:+.3f}  V")
        for ln, box in self._do_boxes.items():
            if ln in aux.get("do", {}):
                box.blockSignals(True)
                box.setChecked(bool(aux["do"][ln]))
                box.blockSignals(False)
