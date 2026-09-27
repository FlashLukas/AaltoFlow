"""Start-up ADOPTS the instrument's state and writes nothing (2026-09-27).

Lukas's rule for every module: "all modules should read the instrument state
on startup, not change anything". For a lock-in that matters: someone may have
set it up by hand in LabOne for a measurement that is running, and a service
start that pushed the .ini would silently change its time constant, reference
or input range under them.

Three things are checked here:
  * start() calls only READ methods on the backend (a recording backend fails
    the test on any setter);
  * status after start reflects a deliberately NON-default pre-existing state
    of the simulated instrument (so adoption is really exercised, not just
    "the defaults happen to match");
  * the real LabOne backend's open() + read_channel() issue no set* call, run
    against a fake `zhinst.core` that raises on any write.
"""

import sys
import types

import pytest

from hf2.backends.sim import SimulatedHF2
from hf2.config import Channel, Config
from hf2.lockin import LockIn

# Everything the brain may call during start(): queries only.
READS = {"open", "idn", "read_channel", "read_demods", "read_aux",
         "pll_locked", "get_time_constant", "get_order"}


class RecordingSim(SimulatedHF2):
    """The simulator, but every backend-interface call is logged, and a write
    while `forbid_writes` is set fails the test on the spot."""

    WRITES = ("setup_channel", "set_reference", "set_oscillator_frequency",
              "set_time_constant", "set_order")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []
        self.forbid_writes = False

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in type(self).WRITES or name in READS:
            calls = super().__getattribute__("calls")
            forbid = super().__getattribute__("forbid_writes")

            def wrapped(*a, **k):
                calls.append(name)
                if forbid and name not in READS:
                    raise AssertionError(f"start() wrote to the instrument: {name}{a}")
                return attr(*a, **k)
            return wrapped
        return attr


def _non_default_instrument(clock=None):
    """A sim left by 'someone in LabOne' in a clearly non-default state."""
    sim = RecordingSim(seed=3) if clock is None else RecordingSim(clock=clock, seed=3)
    sim.preset(Channel(demod=0, signal_input=0, oscillator=0, reference="internal",
                       frequency_Hz=4321.0, time_constant_s=0.03, order=2,
                       harmonic=2, phase_deg=45.0, input_range_V=0.1,
                       input_ac=True, input_50ohm=True), rate_Sa_s=837.1)
    # ch2 (demod 3) follows an EXTERNAL reference on input 0, 300 ms, order 6
    sim.preset(Channel(demod=3, signal_input=1, oscillator=1, reference="external",
                       ref_input=0, frequency_Hz=1234.5, time_constant_s=0.3,
                       order=6, input_diff=True))
    return sim


def test_start_writes_nothing_to_the_instrument():
    sim = _non_default_instrument()
    sim.forbid_writes = True
    li = LockIn(sim, Config())
    li.start(poll=False)
    try:
        li.poll_once()                         # the first poll is reads only too
        assert set(sim.calls) <= READS
        assert "read_channel" in sim.calls
    finally:
        li.shutdown()


def test_status_after_start_is_the_instruments_state():
    cfg = Config()                              # defaults: 10 ms, order 4, internal
    sim = _non_default_instrument()
    li = LockIn(sim, cfg)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        s = li.status()
        assert s.connected
        assert s.reference == ["internal", "external"]
        assert s.tc_set_s == [0.03, 0.3] and s.tc_s == [0.03, 0.3]
        assert s.order == [2, 6]
        assert s.freq_set_Hz[0] == 4321.0
        assert s.demod_enabled == [True, True]
        # cfg IS the adopted state, so Settings and a save show the instrument
        assert cfg.ch1.harmonic == 2 and cfg.ch1.phase_deg == 45.0
        assert cfg.ch1.input_range_V == 0.1 and cfg.ch1.input_ac and cfg.ch1.input_50ohm
        assert cfg.ch2.input_diff and cfg.ch2.ref_input == 0
        assert cfg.hardware.demod_rate_Sa_s == 837.1
        # the instrument itself is untouched
        assert sim.demods[0].tc == 0.03 and sim.demods[3].order == 6
        assert sim.osc_external == [False, True]
        # describe follows: freq2 is an indicator on the adopted external ref
        from hf2.net.describe import build_manifest
        kinds = {p["id"]: p["kind"] for p in build_manifest(li)["parameters"]}
        assert kinds["freq1"] == "control" and kinds["freq2"] == "indicator"
        assert any("nothing changed" in m for _, m in events)
    finally:
        li.shutdown()


def test_ini_values_reach_the_instrument_only_on_apply():
    cfg = Config()
    sim = _non_default_instrument()
    li = LockIn(sim, cfg)
    li.start(poll=False)
    try:
        assert sim.demods[0].tc == 0.03                 # start left it alone
        li.set_time_constant(1, 0.005)                  # an explicit setter writes
        assert sim.demods[0].tc == 0.005
        cfg.ch2.order = 3                               # like Settings > Apply
        li.apply_config()
        assert sim.demods[3].order == 3
    finally:
        li.shutdown()


def test_switched_off_demodulator_is_reported_not_read_nor_enabled():
    sim = _non_default_instrument()
    sim.demods[3].enabled = False                       # ch2's demod is off
    sim.forbid_writes = True
    li = LockIn(sim, Config())
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        li.poll_once()
        s = li.status()
        assert s.demod_enabled == [True, False]
        assert not sim.demods[3].enabled                # still off: we did not switch it on
        assert s.live["r"][1] != s.live["r"][1]         # NaN: no data, not a fake zero
        assert s.live["r"][0] == s.live["r"][0]
        assert any(lvl == "warn" and "switched off" in m for lvl, m in events)
        # an explicit Apply is what switches it on
        sim.forbid_writes = False
        li.apply_config()
        assert sim.demods[3].enabled and li.status().demod_enabled == [True, True]
    finally:
        li.shutdown()


def test_out_of_limit_instrument_value_is_adopted_and_warned():
    sim = _non_default_instrument()
    sim.demods[0].tc = 1000.0                           # above limits.tc_max_s (500)
    li = LockIn(sim, Config())
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        assert li.status().tc_s[0] == 1000.0            # the truth, not a clamp
        assert sim.demods[0].tc == 1000.0
        assert any(lvl == "warn" and "outside" in m for lvl, m in events)
    finally:
        li.shutdown()


# ---- the real LabOne backend against a fake zhinst.core -----------------------

class _FakeDAQ:
    """Answers get* from a node table; any set* is a failed test."""

    def __init__(self, host, port, api_level):
        n = "/dev1234/"
        self.nodes = {
            n + "features/devtype": "HF2LI", n + "features/serial": "dev1234",
            n + "demods/0/adcselect": 0, n + "demods/0/oscselect": 0,
            n + "demods/0/enable": 1, n + "demods/0/harmonic": 1,
            n + "demods/0/phaseshift": 12.0, n + "demods/0/rate": 1842.0,
            n + "demods/0/timeconstant": 0.0471, n + "demods/0/order": 3,
            n + "sigins/0/range": 0.2, n + "sigins/0/ac": 1,
            n + "sigins/0/imp50": 0, n + "sigins/0/diff": 0,
            n + "oscs/0/freq": 777.0,
            n + "plls/0/enable": 1, n + "plls/0/adcselect": 1,
        }

    def connectDevice(self, dev, iface):
        pass                                   # a server-side connection, not a setting

    def _get(self, path):
        if path not in self.nodes:
            raise RuntimeError(f"no node {path}")
        return self.nodes[path]

    getString = getInt = getDouble = _get

    def setInt(self, *a):
        raise AssertionError(f"write at start: setInt{a}")

    def setDouble(self, *a):
        raise AssertionError(f"write at start: setDouble{a}")

    def disconnect(self):
        pass


def test_real_backend_open_and_read_channel_are_queries_only(monkeypatch):
    core = types.ModuleType("zhinst.core")
    core.ziDAQServer = _FakeDAQ
    pkg = types.ModuleType("zhinst")
    pkg.core = core
    monkeypatch.setitem(sys.modules, "zhinst", pkg)
    monkeypatch.setitem(sys.modules, "zhinst.core", core)

    from hf2.backends.zhinst_hf2 import ZhinstHF2
    hw = ZhinstHF2("dev1234")
    hw.open()
    try:
        r = hw.read_channel(0)
        assert r["reference"] == "external" and r["ref_input"] == 1
        assert r["time_constant_s"] == 0.0471 and r["order"] == 3
        assert r["input_range_V"] == 0.2 and r["input_ac"] is True
        assert r["frequency_Hz"] == 777.0 and r["enabled"] is True
    finally:
        hw.close()


def test_real_backend_demod_on_a_non_signal_input_does_not_abort_start(monkeypatch):
    # A demod routed to an aux input (adcselect 2, VERIFY numbering) has no
    # sigins/2 node. Start must still work: the front end is reported None and
    # the brain keeps its configured front-end values, with a warning.
    class _AuxDAQ(_FakeDAQ):
        def __init__(self, *a):
            super().__init__(*a)
            self.nodes["/dev1234/demods/0/adcselect"] = 2

    core = types.ModuleType("zhinst.core")
    core.ziDAQServer = _AuxDAQ
    pkg = types.ModuleType("zhinst")
    pkg.core = core
    monkeypatch.setitem(sys.modules, "zhinst", pkg)
    monkeypatch.setitem(sys.modules, "zhinst.core", core)

    from hf2.backends.zhinst_hf2 import ZhinstHF2
    hw = ZhinstHF2("dev1234")
    hw.open()
    try:
        r = hw.read_channel(0)
        assert r["signal_input"] == 2 and r["input_range_V"] is None
        assert r["time_constant_s"] == 0.0471          # the rest is still read
    finally:
        hw.close()

    # and the brain: keep cfg's front end, warn
    sim = _non_default_instrument()
    orig = sim.read_channel

    def no_front(demod):
        out = dict(orig(demod))
        if demod == 0:
            out.update(input_range_V=None, input_ac=None, input_50ohm=None, input_diff=None)
        return out
    sim.read_channel = no_front
    cfg = Config()
    default_range = cfg.ch1.input_range_V
    li = LockIn(sim, cfg)
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    try:
        assert cfg.ch1.input_range_V == default_range
        assert cfg.ch1.time_constant_s == 0.03          # everything else adopted
        assert any(lvl == "warn" and "could not be read" in m for lvl, m in events)
    finally:
        li.shutdown()


def test_gui_settings_form_shows_the_adopted_state(monkeypatch):
    # The Settings form is built BEFORE start adopts; it must be refilled after,
    # or an Apply pressed at once would push the .ini back over the instrument.
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from hf2.apps.gui import MainWindow

    sim = _non_default_instrument()
    cfg = Config()
    li = LockIn(sim, cfg)
    win = MainWindow(li, cfg)
    try:
        panel = win.inst_tab.settings
        assert panel.apply()                      # Apply pressed straight after start
        # whatever the form holds went to cfg and on to the instrument: it must
        # be the instrument's own state, unchanged
        assert sim.demods[0].tc == pytest.approx(0.03)
        assert sim.demods[3].order == 6
        assert sim.demods[0].harmonic == 2 and sim.sigin[0]["range"] == 0.1
    finally:
        win.timer.stop()
        li.shutdown()
        win.close()
