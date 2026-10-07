"""follow_dialog.py -- set up "this channel's frequency follows another module".

The wire only needs two strings (source "smb.frequency_Hz", formula
"alias(x, 80e6)"), but typing those is hard to get right and harder to read
back (Lukas, 2026-10-07: "hard to understand ... or set up"). So this dialog
builds them from choices a person understands:

  1. FOLLOW  -- a module, then one of the quantities that module REPORTS about
                itself (`describe`: label and unit), so no status key is typed.
  2. RULE    -- "same value", "super-Nyquist (pulsed laser)" with the laser's
                repetition rate and the harmonic as numbers, or a custom formula.
  3. CHECK   -- type a test value and see the frequency it gives, at once.

Plus PRESETS: named setups saved in follow_presets.json next to this module
(this PC only; two built in). Every widget explains itself on hover.

The dialog never talks to the hf2 service itself: it returns (source, formula,
endpoint) and the caller sends set_follow. It does talk to the SOURCE module,
once, to read its `describe` -- the same request any client makes.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ..follow import Formula, parse_source, resolve_endpoint

#: the module's folder (src/hf2/apps -> hf2-control): presets live there
MODULE_DIR = Path(__file__).resolve().parents[3]
PRESETS_FILE = MODULE_DIR / "follow_presets.json"

#: always offered, cannot be deleted
BUILTIN_PRESETS = [
    {"name": "Super-Nyquist, 80 MHz laser (SMB)", "source": "smb.frequency_Hz",
     "formula": "alias(x, 80e6)", "endpoint": ""},
    {"name": "Same as SMB frequency", "source": "smb.frequency_Hz",
     "formula": "", "endpoint": ""},
]

RULE_SAME, RULE_ALIAS, RULE_FOLD, RULE_CUSTOM = range(4)
_RULE_NAMES = ["Same value", "Super-Nyquist: nearest alias (pulsed laser)",
               "Super-Nyquist: keep the side (f mod rep rate)", "Custom formula"]
_RULE_RE = re.compile(r"^\s*(alias|fold)\(\s*(?:(\d+)\s*\*\s*)?x\s*,\s*([0-9.eE+-]+)\s*\)\s*$")

_FREQ_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}


# ------------------------------------------------------------ plain words ---

def parse_rule(formula: str):
    """formula -> (rule, rep_rate_Hz, harmonic). Custom when not recognised."""
    f = str(formula or "").strip()
    if not f or f == "x":
        return RULE_SAME, 80e6, 1
    m = _RULE_RE.match(f)
    if m:
        try:
            return (RULE_ALIAS if m.group(1) == "alias" else RULE_FOLD,
                    float(m.group(3)), int(m.group(2) or 1))
        except ValueError:
            pass
    return RULE_CUSTOM, 80e6, 1


def build_formula(rule: int, rep_Hz: float, harmonic: int, custom: str) -> str:
    if rule == RULE_SAME:
        return ""
    if rule in (RULE_ALIAS, RULE_FOLD):
        fn = "alias" if rule == RULE_ALIAS else "fold"
        n = f"{int(harmonic)}*" if int(harmonic) != 1 else ""
        return f"{fn}({n}x, {rep_Hz:g})"
    return str(custom or "").strip()


def explain(source: str, formula: str) -> str:
    """One readable line for the channel card, e.g.
    'smb.frequency_Hz -> super-Nyquist, 80 MHz laser'."""
    if not str(source or "").strip():
        return "not set up"
    rule, rep, n = parse_rule(formula)
    if rule == RULE_SAME:
        how = "same value"
    elif rule == RULE_CUSTOM:
        how = f"formula {formula}"
    else:
        how = (f"super-Nyquist, {rep / 1e6:g} MHz laser"
               + (f", harmonic {n}" if n != 1 else "")
               + (", keep side" if rule == RULE_FOLD else ""))
    return f"{source}  ->  {how}"


# ---------------------------------------------------------------- presets ---

def load_presets(path: Path = PRESETS_FILE) -> list[dict]:
    """Built-ins first, then this PC's saved ones (a broken file is ignored)."""
    saved = []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for p in data if isinstance(data, list) else []:
            if isinstance(p, dict) and p.get("name") and p.get("source"):
                saved.append({"name": str(p["name"]), "source": str(p["source"]),
                              "formula": str(p.get("formula", "")),
                              "endpoint": str(p.get("endpoint", ""))})
    except (OSError, ValueError):
        pass
    return [dict(p, builtin=True) for p in BUILTIN_PRESETS] + saved


def save_presets(saved: list[dict], path: Path = PRESETS_FILE) -> None:
    """Write the user's presets (not the built-ins), atomically."""
    rows = [{k: p[k] for k in ("name", "source", "formula", "endpoint")}
            for p in saved if not p.get("builtin")]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    os.replace(tmp, path)


# -------------------------------------------------------- module discovery ---

def known_modules() -> dict:
    """{key: (name, endpoint)} -- where the modules can be found.

    From the launcher's table (AALTOFLOW_ENDPOINTS) when this GUI was started
    from Mission Control; otherwise from the module.toml files of this
    checkout with their DEFAULT ports (a module started by hand). endpoint ""
    means "the service will find it in the launcher's table too".
    """
    out = {}
    root = MODULE_DIR.parents[1]                      # .../modules
    try:
        import tomllib
        for mt in sorted(root.glob("*/*/module.toml")):
            try:
                with open(mt, "rb") as fh:
                    d = tomllib.load(fh)
                m, ports = d.get("module", {}), d.get("ports", {})
                key = m.get("key")
                if key and ports.get("cmd") and ports.get("pub"):
                    out[key] = (m.get("name", key),
                                f"127.0.0.1:{int(ports['cmd'])}:{int(ports['pub'])}")
            except (OSError, ValueError, TypeError):
                continue
    except ImportError:
        pass
    raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
    try:
        table = json.loads(raw) if raw else {}
    except ValueError:
        table = {}
    for key in table if isinstance(table, dict) else ():
        out[key] = (out.get(key, (key, ""))[0], "")
    out.pop("hf2", None)              # following ourselves makes no sense
    return out


def fetch_quantities(module: str, endpoint: str, timeout_ms: int = 1500) -> list[dict]:
    """The numbers `module` reports: [{label, unit, scale, path}], from its
    describe. Raises ValueError (in words) when it does not answer."""
    import zmq
    from .. import secure
    host, cmd, _pub = resolve_endpoint(module, endpoint)
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    try:
        secure.secure_client(s, host, module)
        s.connect(f"tcp://{host}:{cmd}")
        s.send(json.dumps({"cmd": "describe"}).encode("utf-8"))
        try:
            reply = json.loads(s.recv())
        except zmq.Again:
            raise ValueError(f"{module} is not running (no answer at {host}:{cmd})") from None
    finally:
        s.close(0)
    params = (reply.get("describe") or {}).get("parameters", []) if reply.get("ok") else []
    out = []
    for p in params:
        path = p.get("read_path")
        if p.get("type") not in ("float", "int") or not path:
            continue
        out.append({"label": p.get("label", p.get("id")), "unit": p.get("unit", ""),
                    "scale": float(p.get("scale") or 1.0),
                    "path": ".".join(str(k) for k in path)})
    return out


# ------------------------------------------------------------------ dialog ---

class FollowDialog(QtWidgets.QDialog):
    """Returns the chosen setup in .source / .formula / .endpoint on accept."""

    def __init__(self, channel: int, source: str, formula: str, endpoint: str,
                 parent=None, presets_path: Path = PRESETS_FILE):
        super().__init__(parent)
        self.setWindowTitle(f"Channel {channel} -- follow another module")
        self.setMinimumWidth(560)
        self._presets_path = presets_path
        self._modules = known_modules()
        self._quantities: list[dict] = []
        self.source, self.formula, self.endpoint = source, formula, endpoint
        self._endpoint, self._endpoint_key = endpoint, None

        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            f"Channel {channel}'s demodulation frequency is computed from another "
            f"module's value, and recomputed every time that value changes -- from a "
            f"scan, a script or that module's own window.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # -- presets -------------------------------------------------------
        pr = QtWidgets.QHBoxLayout()
        pr.addWidget(QtWidgets.QLabel("Preset"))
        self.preset = QtWidgets.QComboBox()
        self.preset.setToolTip("Saved setups. Choosing one fills in everything below; "
                               "nothing is applied until you press OK.")
        self.preset.activated.connect(self._preset_chosen)
        pr.addWidget(self.preset, 1)
        self.btn_save = QtWidgets.QPushButton("Save as...")
        self.btn_save.setToolTip("Save the setup below under a name (this PC, "
                                 "follow_presets.json in the hf2-control folder).")
        self.btn_save.clicked.connect(self._save_preset)
        self.btn_del = QtWidgets.QPushButton("Delete")
        self.btn_del.setToolTip("Delete the selected preset (built-in presets stay).")
        self.btn_del.clicked.connect(self._delete_preset)
        pr.addWidget(self.btn_save); pr.addWidget(self.btn_del)
        lay.addLayout(pr)

        # -- 1 follow ----------------------------------------------------------
        g1 = QtWidgets.QGroupBox("1  Follow")
        f1 = QtWidgets.QFormLayout(g1)
        self.module = QtWidgets.QComboBox(); self.module.setEditable(True)
        for key, (name, _ep) in sorted(self._modules.items()):
            self.module.addItem(f"{key}  -  {name}", key)
        self.module.setToolTip("The module whose value is followed, e.g. smb (the RF "
                               "generator). It has to be running to list what it reports.")
        self.module.activated.connect(lambda _i: self._load_quantities())
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.setToolTip("Ask the module again what it reports (start it first).")
        self.btn_refresh.clicked.connect(self._load_quantities)
        mrow = QtWidgets.QHBoxLayout(); mrow.addWidget(self.module, 1); mrow.addWidget(self.btn_refresh)
        f1.addRow("Module", mrow)
        self.quantity = QtWidgets.QComboBox(); self.quantity.setEditable(True)
        self.quantity.setToolTip(
            "Which of its values to follow -- the list comes from the module itself.\n"
            "If it is not running you can type the status key instead "
            "(e.g. frequency_Hz).")
        self.quantity.currentIndexChanged.connect(lambda _i: self._update_check())
        self.quantity.editTextChanged.connect(lambda _t: self._update_check())
        f1.addRow("Quantity", self.quantity)
        self.q_note = QtWidgets.QLabel(""); self.q_note.setWordWrap(True)
        self.q_note.setObjectName("hint")
        f1.addRow("", self.q_note)
        lay.addWidget(g1)

        # -- 2 rule ------------------------------------------------------------
        g2 = QtWidgets.QGroupBox("2  Rule")
        f2 = QtWidgets.QFormLayout(g2)
        self.rule = QtWidgets.QComboBox(); self.rule.addItems(_RULE_NAMES)
        self.rule.setItemData(RULE_SAME, "Demodulate at exactly the followed frequency.",
                              QtCore.Qt.ToolTipRole)
        self.rule.setItemData(RULE_ALIAS,
            "The pulsed laser samples the signal: f lands at its distance to the nearest\n"
            "multiple of the repetition rate. 810 MHz with 80 MHz -> 10 MHz;\n"
            "790 MHz -> 10 MHz too (the mirror line, phase sign flipped).",
            QtCore.Qt.ToolTipRole)
        self.rule.setItemData(RULE_FOLD,
            "f modulo the repetition rate: 810 MHz -> 10 MHz but 790 MHz -> 70 MHz.\n"
            "Use it when the side of the laser harmonic matters.", QtCore.Qt.ToolTipRole)
        self.rule.setItemData(RULE_CUSTOM,
            "Any arithmetic in x (the followed value, in its base unit, e.g. Hz).\n"
            "Functions: alias(f, fs), fold(f, fs), abs, round, min, max, floor, ceil, sqrt.",
            QtCore.Qt.ToolTipRole)
        self.rule.setToolTip("How the demodulation frequency is computed from the "
                             "followed value. Hover over each choice for details.")
        self.rule.currentIndexChanged.connect(self._rule_changed)
        f2.addRow("Rule", self.rule)
        self.rep = QtWidgets.QDoubleSpinBox(); self.rep.setRange(0.001, 1e5)
        self.rep.setDecimals(3); self.rep.setSuffix(" MHz")
        self.rep.setToolTip("The laser's pulse repetition rate (80 MHz on the TR-MOKE setup).")
        self.rep.valueChanged.connect(lambda _v: self._update_check())
        f2.addRow("Laser rep. rate", self.rep)
        self.harm = QtWidgets.QSpinBox(); self.harm.setRange(1, 100)
        self.harm.setToolTip("Demodulate the n-th harmonic of the followed frequency "
                             "(1 = the fundamental).")
        self.harm.valueChanged.connect(lambda _v: self._update_check())
        f2.addRow("Harmonic n", self.harm)
        self.custom = QtWidgets.QLineEdit()
        self.custom.setPlaceholderText("e.g.  abs(x - 800e6)")
        self.custom.setToolTip(self.rule.itemData(RULE_CUSTOM, QtCore.Qt.ToolTipRole))
        self.custom.textChanged.connect(lambda _t: self._update_check())
        f2.addRow("Formula", self.custom)
        lay.addWidget(g2)
        self._f2 = f2

        # -- 3 check -----------------------------------------------------------
        g3 = QtWidgets.QGroupBox("3  Check")
        f3 = QtWidgets.QHBoxLayout(g3)
        f3.addWidget(QtWidgets.QLabel("If it reads"))
        self.test = QtWidgets.QDoubleSpinBox(); self.test.setRange(-1e12, 1e12)
        self.test.setDecimals(3); self.test.setValue(810.0)
        self.test.setToolTip("Try a value of the followed quantity, in its own unit.")
        self.test.valueChanged.connect(lambda _v: self._update_check())
        f3.addWidget(self.test)
        self.outcome = QtWidgets.QLabel("")
        self.outcome.setToolTip("What the lock-in would be set to. 1 Hz .. 50 MHz is "
                               "allowed; anything else is refused, not clamped.")
        f3.addWidget(self.outcome, 1)
        lay.addWidget(g3)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                          | QtWidgets.QDialogButtonBox.Cancel)
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Use this")
        btns.button(QtWidgets.QDialogButtonBox.Ok).setToolTip(
            "Store this setup for the channel. If the channel is following, it "
            "follows the new setup at once; otherwise tick Follow on the card.")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._reload_presets()
        self._fill(source, formula, endpoint)

    # -- filling ---------------------------------------------------------------

    def _fill(self, source: str, formula: str, endpoint: str) -> None:
        try:
            mod, path = parse_source(source) if source else ("smb", ["frequency_Hz"])
        except ValueError:
            mod, path = "smb", ["frequency_Hz"]
        # an endpoint typed earlier belongs to THAT module only
        self._endpoint, self._endpoint_key = endpoint, (mod if endpoint else None)
        i = self.module.findData(mod)
        if i < 0:
            self.module.addItem(mod, mod); i = self.module.count() - 1
        self.module.setCurrentIndex(i)
        self._load_quantities(select=".".join(str(k) for k in path))
        rule, rep, n = parse_rule(formula)
        self.rule.setCurrentIndex(rule)
        self.rep.setValue(rep / 1e6)
        self.harm.setValue(n)
        self.custom.setText(formula if rule == RULE_CUSTOM else "")
        self._rule_changed(rule)

    def _module_key(self) -> str:
        d = self.module.currentData()
        if d and self.module.currentText().startswith(str(d)):
            return str(d)
        return self.module.currentText().split()[0] if self.module.currentText().strip() else ""

    def _module_endpoint(self, key: str) -> str:
        if self._endpoint and self._endpoint_key == key:
            return self._endpoint
        return self._modules.get(key, ("", ""))[1]

    def _load_quantities(self, select: str | None = None) -> None:
        key = self._module_key()
        current = select or self._path()
        self.quantity.blockSignals(True)
        self.quantity.clear()
        self._quantities = []
        try:
            self._quantities = fetch_quantities(key, self._module_endpoint(key))
            for q in self._quantities:
                unit = f" ({q['unit']})" if q["unit"] else ""
                self.quantity.addItem(f"{q['label']}{unit}", q["path"])
            self.q_note.setText(f"{len(self._quantities)} values reported by {key}.")
        except ValueError as exc:
            self.q_note.setText(f"{exc}. Start it and press Refresh, or type the "
                                f"status key (e.g. frequency_Hz).")
        i = self.quantity.findData(current) if current else -1
        if i >= 0:
            self.quantity.setCurrentIndex(i)
        elif current:
            self.quantity.setEditText(current)
        self.quantity.blockSignals(False)
        self._update_check()

    def _path(self) -> str:
        d = self.quantity.currentData()
        if d is not None and self.quantity.currentText() == self.quantity.itemText(
                self.quantity.currentIndex()):
            return str(d)
        return self.quantity.currentText().strip()

    def _scale_unit(self) -> tuple[float, str]:
        path = self._path()
        for q in self._quantities:
            if q["path"] == path:
                return q["scale"], q["unit"]
        return 1.0, ""

    def _rule_changed(self, rule: int) -> None:
        sn = rule in (RULE_ALIAS, RULE_FOLD)
        for w, on in ((self.rep, sn), (self.harm, sn), (self.custom, rule == RULE_CUSTOM)):
            w.setVisible(on)
            self._f2.labelForField(w).setVisible(on)
        self._update_check()

    def _current(self) -> tuple[str, str, str]:
        key = self._module_key()
        src = f"{key}.{self._path()}" if key and self._path() else ""
        fml = build_formula(self.rule.currentIndex(), self.rep.value() * 1e6,
                            self.harm.value(), self.custom.text())
        ep = self._endpoint if (self._endpoint and self._endpoint_key == key) else ""
        if not ep and key in self._modules and not self._from_launcher(key):
            ep = self._modules[key][1]      # started by hand: tell the service where
        return src, fml, ep

    @staticmethod
    def _from_launcher(key: str) -> bool:
        raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
        try:
            return key in (json.loads(raw) if raw else {})
        except ValueError:
            return False

    def _update_check(self) -> None:
        scale, unit = self._scale_unit()
        self.test.setSuffix(f" {unit}" if unit else "")
        src, fml, _ep = self._current()
        try:
            if not src:
                raise ValueError("choose what to follow")
            parse_source(src)
            hz = Formula(fml)(self.test.value() * scale)
            ok = 1.0 <= hz <= 50e6
            self.outcome.setText(f"->  demodulate at {_fmt_hz(hz)}"
                                + ("" if ok else "  (outside 1 Hz .. 50 MHz: refused)"))
            self.outcome.setStyleSheet("" if ok else "color:#ff5c5c;")
        except ValueError as exc:
            self.outcome.setText(str(exc))
            self.outcome.setStyleSheet("color:#ff5c5c;")
        self.preset.setToolTip(f"Saved setups. Current: {explain(src, fml)}")

    # -- presets ------------------------------------------------------------------

    def _reload_presets(self, select: str | None = None) -> None:
        self._presets = load_presets(self._presets_path)
        self.preset.clear()
        self.preset.addItem("(choose a preset)", None)
        for p in self._presets:
            self.preset.addItem(p["name"] + ("" if p.get("builtin") else "  *"), p["name"])
            self.preset.setItemData(self.preset.count() - 1,
                                    explain(p["source"], p["formula"])
                                    + ("" if p.get("builtin") else "\n(saved on this PC)"),
                                    QtCore.Qt.ToolTipRole)
        if select:
            i = self.preset.findData(select)
            if i >= 0:
                self.preset.setCurrentIndex(i)

    def _preset(self) -> dict | None:
        name = self.preset.currentData()
        return next((p for p in self._presets if p["name"] == name), None)

    def _preset_chosen(self, _i) -> None:
        p = self._preset()
        if p:
            self._fill(p["source"], p["formula"], p["endpoint"])

    def _save_preset(self) -> None:
        src, fml, ep = self._current()
        if not src:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Save preset", "Name:",
                                                  text=explain(src, fml))
        name = name.strip()
        if not ok or not name:
            return
        if any(p["name"] == name and p.get("builtin") for p in self._presets):
            QtWidgets.QMessageBox.warning(self, "Save preset",
                                          f"'{name}' is a built-in preset; choose another name.")
            return
        saved = [p for p in self._presets if not p.get("builtin") and p["name"] != name]
        saved.append({"name": name, "source": src, "formula": fml, "endpoint": ep})
        try:
            save_presets(saved, self._presets_path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Save preset", f"could not save: {exc}")
            return
        self._reload_presets(select=name)

    def _delete_preset(self) -> None:
        p = self._preset()
        if not p or p.get("builtin"):
            return
        saved = [q for q in self._presets if not q.get("builtin") and q["name"] != p["name"]]
        try:
            save_presets(saved, self._presets_path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Delete preset", f"could not save: {exc}")
            return
        self._reload_presets()

    def _accept(self) -> None:
        src, fml, ep = self._current()
        try:
            parse_source(src)
            Formula(fml)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Follow", str(exc))
            return
        self.source, self.formula, self.endpoint = src, fml, ep
        self.accept()


def _fmt_hz(f: float) -> str:
    if not math.isfinite(f):
        return "--"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")):
        if abs(f) >= scale or scale == 1.0:
            return f"{f / scale:.6g} {unit}"
