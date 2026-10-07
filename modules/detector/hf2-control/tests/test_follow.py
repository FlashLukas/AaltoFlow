"""A channel's frequency FOLLOWING another module (follow.py, 2026-10-07).

The case that asked for it: super-Nyquist MOKE, 80 MHz laser, RF from the SMB.
810 MHz must be demodulated at 10 MHz, and every RF change must move the
demodulation with it. A fake "smb" (REP + PUB on scratch ports, no smb import)
plays the generator.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import zmq

from hf2.config import Config
from hf2.follow import Formula, alias, fold, parse_source, read_path, resolve_endpoint
from hf2.net.describe import build_manifest
from hf2.sim_system import build_sim_system

SRC_CMD, SRC_PUB = 15910, 15911          # the fake generator
DEAD_CMD, DEAD_PUB = 15912, 15913        # nobody listens here
SVC_CMD, SVC_PUB = 15914, 15915          # the hf2 service in the wire test
EP = f"127.0.0.1:{SRC_CMD}:{SRC_PUB}"


class FakeGenerator:
    """Answers `status` and publishes {"frequency_Hz": f} at 20 Hz -- what
    smb-control does, as far as a follower can tell."""

    def __init__(self, f_Hz: float):
        self.f = f_Hz
        self.publish = True
        self._stop = threading.Event()
        ctx = zmq.Context.instance()
        self._rep = ctx.socket(zmq.REP); self._rep.bind(f"tcp://127.0.0.1:{SRC_CMD}")
        self._pub = ctx.socket(zmq.PUB); self._pub.bind(f"tcp://127.0.0.1:{SRC_PUB}")
        self._t = threading.Thread(target=self._run, daemon=True); self._t.start()

    def _run(self):
        poller = zmq.Poller(); poller.register(self._rep, zmq.POLLIN)
        next_pub = 0.0
        while not self._stop.is_set():
            if poller.poll(10):
                req = json.loads(self._rep.recv())
                if req.get("cmd") == "describe":      # what smb's describe says about it
                    reply = {"ok": True, "describe": {"module": "smb", "parameters": [
                        {"id": "frequency", "label": "Frequency", "type": "float",
                         "unit": "MHz", "scale": 1e6, "read_path": ["frequency_Hz"]},
                        {"id": "rf", "label": "RF on", "type": "bool", "read_path": ["rf_on"]}]}}
                else:
                    reply = {"ok": True, "status": {"frequency_Hz": self.f}}
                self._rep.send(json.dumps(reply).encode())
            if self.publish and time.monotonic() >= next_pub:
                next_pub = time.monotonic() + 0.05
                self._pub.send_multipart([b"status", json.dumps({"frequency_Hz": self.f}).encode()])
        self._rep.close(0); self._pub.close(0)

    def close(self):
        self._stop.set(); self._t.join(2)


@pytest.fixture
def gen():
    g = FakeGenerator(810e6)
    yield g
    g.close()


@pytest.fixture
def li():
    lockin, _sim = build_sim_system(Config(), seed=1)
    events = []
    lockin._on_event = lambda lvl, msg: events.append((lvl, msg))
    lockin.events = events
    lockin.start(poll=False)
    yield lockin
    lockin.shutdown()


def _wait(cond, timeout=3.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---- the formula ---------------------------------------------------------------

def test_alias_is_the_super_nyquist_case():
    assert alias(810e6, 80e6) == pytest.approx(10e6)
    assert alias(790e6, 80e6) == pytest.approx(10e6)       # the mirror image
    assert fold(790e6, 80e6) == pytest.approx(70e6)        # fold keeps the side
    assert Formula("alias(x, 80e6)")(810e6) == pytest.approx(10e6)
    assert Formula("alias(2*x, 80e6)")(405e6) == pytest.approx(10e6)   # 2nd harmonic
    assert Formula("")(123.0) == 123.0                     # empty = the value itself
    assert Formula("abs(x - 800e6) / 2")(810e6) == pytest.approx(5e6)


@pytest.mark.parametrize("bad", [
    "__import__('os')", "x.real", "open('f')", "y + 1", "'text'", "x if x else 1",
    "alias(x, fs=80e6)", "x ** 10 ** 10", "[x]", "x +",
])
def test_formula_refuses_anything_but_arithmetic(bad):
    with pytest.raises(ValueError):
        Formula(bad)(1.0)


def test_source_and_endpoint(monkeypatch):
    assert parse_source("smb.frequency_Hz") == ("smb", ["frequency_Hz"])
    assert parse_source("hf2.ref_freq_Hz.1") == ("hf2", ["ref_freq_Hz", 1])
    with pytest.raises(ValueError):
        parse_source("smb")
    assert read_path({"a": [1, 2.5]}, ["a", 1]) == 2.5
    assert read_path({"a": True}, ["a"]) is None             # a bool is not a number
    assert resolve_endpoint("smb", "pc:1:2") == ("pc", 1, 2)
    monkeypatch.setenv("AALTOFLOW_ENDPOINTS", json.dumps({"smb": ["localhost", 5557, 5558]}))
    assert resolve_endpoint("smb") == ("127.0.0.1", 5557, 5558)
    with pytest.raises(ValueError, match="Mission Control"):
        resolve_endpoint("windfreak")


# ---- the lock-in following a generator ------------------------------------------

def test_follows_the_generator_live(li, gen):
    li.set_follow(1, True, "smb.frequency_Hz", "alias(x, 80e6)", EP)
    # applied AT ONCE (sync), not only at the next frame
    assert li.cfg.ch1.frequency_Hz == pytest.approx(10e6)
    assert li.backend.osc_freq[li.cfg.ch1.oscillator] == pytest.approx(10e6)
    st = li.status()
    assert st.follow_on == [True, False]
    assert st.follow[0]["x"] == 810e6 and st.follow[0]["target"] == pytest.approx(10e6)
    gen.f = 830e6                                     # the RF moves ...
    assert _wait(lambda: li.cfg.ch1.frequency_Hz == pytest.approx(30e6))   # ... so does the lock-in
    assert li.cfg.ch2.frequency_Hz != pytest.approx(30e6)                 # ch2 untouched


def test_hand_set_and_external_refused_while_following(li, gen):
    li.set_follow(1, True, "smb.frequency_Hz", "alias(x, 80e6)", EP)
    with pytest.raises(ValueError, match="Follow off"):
        li.set_frequency(1, 1e3)
    with pytest.raises(ValueError, match="Follow off"):
        li.set_reference(1, "external")
    li.set_follow(1, False)
    assert li.status().follow_on == [False, False]
    li.set_frequency(1, 1e3)                          # works again
    gen.f = 850e6
    time.sleep(0.2)
    assert li.cfg.ch1.frequency_Hz == 1e3             # and nothing follows any more


def test_acquire_asks_the_source_first(li, gen):
    """The scan race: scan-core saw the generator report its new frequency and
    triggers at once -- before our subscription heard it. acquire must not
    start the settle clock at the old demodulation frequency."""
    li.set_follow(1, True, "smb.frequency_Hz", "alias(x, 80e6)", EP)
    gen.publish = False                               # the stream is "late"
    time.sleep(0.1)
    gen.f = 850e6
    li.acquire()
    assert li.cfg.ch1.frequency_Hz == pytest.approx(30e6)   # |850 - 11*80| MHz


def test_acquire_refused_when_the_source_is_silent(li):
    li.set_follow(1, True, "smb.frequency_Hz", "alias(x, 80e6)",
                  f"127.0.0.1:{DEAD_CMD}:{DEAD_PUB}")
    assert any("as soon as" in m for _, m in li.events)      # following, waiting
    with pytest.raises(ValueError, match="did not answer"):
        li.acquire()


def test_out_of_range_result_is_refused_not_clamped(li, gen):
    f0 = li.cfg.ch1.frequency_Hz
    li.set_follow(1, True, "smb.frequency_Hz", "x", EP)      # 810 MHz > HF2's 50 MHz
    assert li.cfg.ch1.frequency_Hz == f0
    assert "outside" in li.status().follow[0]["error"]
    with pytest.raises(ValueError, match="outside"):
        li.acquire()


def test_bad_follow_settings_refused_before_any_change(li):
    with pytest.raises(ValueError):
        li.set_follow(1, True, "smb.frequency_Hz", "import os")
    with pytest.raises(ValueError):
        li.set_follow(1, True, "nodot")
    assert li.cfg.follow.ch1_source == "" and li.status().follow_on == [False, False]
    li.set_reference(2, "external")
    with pytest.raises(ValueError, match="EXTERNAL"):
        li.set_follow(2, True, "smb.frequency_Hz", "", EP)
    bad = Config(); bad.follow.ch1_formula = "open('x')"
    with pytest.raises(ValueError):
        li.check_config(bad)


def test_start_does_not_follow(gen):
    """Adopt on start: a source in the .ini does not switch following on."""
    cfg = Config()
    cfg.follow.ch1_source, cfg.follow.ch1_formula = "smb.frequency_Hz", "alias(x, 80e6)"
    cfg.follow.ch1_endpoint = EP
    lockin, sim = build_sim_system(cfg, seed=1)
    lockin.start(poll=False)
    try:
        time.sleep(0.2)
        assert lockin.status().follow_on == [False, False]
        assert lockin.cfg.ch1.frequency_Hz != pytest.approx(10e6)
        lockin.set_follow(1, True)                     # the stored source + formula
        assert lockin.cfg.ch1.frequency_Hz == pytest.approx(10e6)
    finally:
        lockin.shutdown()


def test_describe_freq_becomes_an_indicator(li, gen):
    def kinds():
        return {p["id"]: p["kind"] for p in build_manifest(li)["parameters"]}
    assert kinds()["freq1"] == "control" and kinds()["follow1"] == "control"
    rev0 = build_manifest(li)["revision"]
    li.set_follow(1, True, "smb.frequency_Hz", "alias(x, 80e6)", EP)
    assert kinds()["freq1"] == "indicator"            # a scan cannot set it now
    assert build_manifest(li)["revision"] != rev0     # clients re-fetch


def test_over_the_wire(gen):
    from hf2.net.client import Hf2Client
    from hf2.net.service import Hf2Service
    lockin, _ = build_sim_system(Config(), seed=1)
    svc = Hf2Service(lockin, host="127.0.0.1", cmd_port=SVC_CMD, pub_port=SVC_PUB)
    svc.start()
    cli = Hf2Client(host="127.0.0.1", cmd_port=SVC_CMD, pub_port=SVC_PUB)
    try:
        r = cli.set_follow(2, True, "smb.frequency_Hz", "alias(x, 80e6)", EP)
        assert r["ok"], r
        r = cli._cmd({"cmd": "status"})
        assert r["status"]["follow_on"] == [False, True]
        assert r["status"]["freq_set_Hz"][1] == pytest.approx(10e6)
        r = cli.set_frequency(2, 1e3)
        assert not r["ok"] and "Follow off" in r["error"]
        r = cli.set_follow(2, True, "smb.frequency_Hz", "import os")
        assert not r["ok"]
    finally:
        cli.shutdown()
        svc.stop()


def test_rule_words_round_trip():
    from hf2.apps.follow_dialog import (RULE_ALIAS, RULE_CUSTOM, RULE_FOLD, RULE_SAME,
                                        build_formula, explain, parse_rule)
    assert parse_rule("alias(x, 80e6)") == (RULE_ALIAS, 80e6, 1)
    assert parse_rule("fold(2*x, 76e6)") == (RULE_FOLD, 76e6, 2)
    assert parse_rule("") == (RULE_SAME, 80e6, 1)
    assert parse_rule("abs(x - 1)")[0] == RULE_CUSTOM
    assert build_formula(RULE_ALIAS, 80e6, 1, "") == "alias(x, 8e+07)"
    assert Formula(build_formula(RULE_ALIAS, 80e6, 1, ""))(810e6) == pytest.approx(10e6)
    assert parse_rule(build_formula(RULE_FOLD, 80e6, 3, "")) == (RULE_FOLD, 80e6, 3)
    assert explain("smb.frequency_Hz", "alias(x, 80e6)") ==         "smb.frequency_Hz  ->  super-Nyquist, 80 MHz laser"
    assert explain("", "") == "not set up"


def test_presets_file(tmp_path):
    from hf2.apps.follow_dialog import BUILTIN_PRESETS, load_presets, save_presets
    f = tmp_path / "follow_presets.json"
    assert len(load_presets(f)) == len(BUILTIN_PRESETS)          # no file: built-ins
    mine = {"name": "76 MHz laser", "source": "smb.frequency_Hz",
            "formula": "alias(x, 76e6)", "endpoint": ""}
    save_presets(load_presets(f) + [mine], f)
    got = load_presets(f)
    assert got[-1]["name"] == "76 MHz laser" and not got[-1].get("builtin")
    assert len(json.loads(f.read_text(encoding="utf-8"))) == 1   # built-ins not written
    f.write_text("not json", encoding="utf-8")
    assert len(load_presets(f)) == len(BUILTIN_PRESETS)          # broken file ignored


def _qt():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_follow_dialog(gen, tmp_path, monkeypatch):
    """Pick the module's quantity from ITS describe, choose the rule, check it,
    save a preset -- and get back the two strings the wire needs."""
    _qt()
    from PySide6 import QtWidgets
    from hf2.apps.follow_dialog import RULE_FOLD, FollowDialog
    presets = tmp_path / "p.json"
    dlg = FollowDialog(1, "smb.frequency_Hz", "alias(x, 80e6)", EP, presets_path=presets)
    assert dlg.quantity.count() == 1                       # the bool is not offered
    assert dlg.quantity.currentText() == "Frequency (MHz)"
    assert dlg.test.suffix() == " MHz"
    assert "10 MHz" in dlg.outcome.text()                   # 810 MHz -> 10 MHz
    dlg.rule.setCurrentIndex(RULE_FOLD)
    dlg.test.setValue(790)
    assert "70 MHz" in dlg.outcome.text()
    dlg.rep.setValue(76)
    dlg.harm.setValue(2)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("76 MHz, 2nd", True)))
    dlg._save_preset()
    assert json.loads(presets.read_text(encoding="utf-8"))[0]["formula"] == "fold(2*x, 7.6e+07)"
    dlg._accept()
    assert dlg.result() == QtWidgets.QDialog.Accepted
    assert (dlg.source, dlg.formula, dlg.endpoint) ==         ("smb.frequency_Hz", "fold(2*x, 7.6e+07)", EP)
    # a preset fills everything in again
    dlg2 = FollowDialog(1, "", "", "", presets_path=presets)
    dlg2.preset.setCurrentIndex(dlg2.preset.findData("76 MHz, 2nd"))
    dlg2._preset_chosen(0)
    assert dlg2.rule.currentIndex() == RULE_FOLD and dlg2.harm.value() == 2


def test_gui_follow_row(gen):
    """The card says in words what is followed; ticking Follow follows and
    locks the frequency box; unticking frees it."""
    _qt()
    from PySide6 import QtWidgets
    from hf2.apps.gui import MainWindow
    cfg = Config()
    lockin, _ = build_sim_system(cfg, seed=5)
    win = MainWindow(lockin, cfg)
    try:
        ch1 = win.channels[0]
        win._refresh()
        assert ch1.follow_what.text() == "not set up"
        lockin.set_follow(1, False, "smb.frequency_Hz", "alias(x, 80e6)", EP)  # = dialog OK
        win._refresh()
        assert "super-Nyquist, 80 MHz laser" in ch1.follow_what.text()
        ch1.follow_box.click()                       # the user ticks Follow
        win._refresh()
        assert lockin.status().follow_on[0]
        assert not ch1.freq_set.isEnabled()
        assert "10 MHz" in ch1.follow_info.text()
        ch1.follow_box.click()                       # and unticks it
        win._refresh()
        assert not lockin.status().follow_on[0] and ch1.freq_set.isEnabled()
        QtWidgets.QApplication.instance().processEvents()
    finally:
        win.close()
        lockin.shutdown()
