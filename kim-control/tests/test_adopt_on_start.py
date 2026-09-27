"""Adopt-on-start (Lukas, 2026-09-27): starting the software must not change the stage.

Until then `Kim.start()` and `KinesisKim.open()` both PUSHED the .ini's drive
parameters (85 V / 300 steps/s / 5000 steps/s^2) onto every channel -- on the
lab unit that silently replaced the 112 V / 500 / 1000 someone had set in
Kinesis. Now start-up only READS, and the brain adopts what it finds.

The "controller" in these tests starts from a state the config would never
produce (the numbers actually found on the lab KIM101 on 2026-09-13), so a
brain that still pushed its config would be caught by the status, not only by
the write log.
"""

from __future__ import annotations

import sys
import types

from kim.backends.kinesis_kim import KinesisKim
from kim.backends.sim import SimKim
from kim.config import Config
from kim.kim import Kim

from test_kinesis_backend import FakeKim101

# What the lab KIM101 held before we first touched it (CLAUDE.local.md).
FOUND = {"position": [31, 667, 183], "rate": [500, 500, 500],
         "accel": [1000, 1000, 1000], "voltage": [112, 112, 112]}

# Calls that only READ the controller. Anything else during start is a write.
READS = {"get_position", "is_moving", "get_drive_parameters", "get_device_info"}


def _sim_brain(cfg: Config | None = None):
    cfg = cfg or Config()
    backend = SimKim(cfg, state=FOUND)
    brain = Kim(backend, cfg)
    events: list = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    return brain, backend, events


def test_sim_start_writes_nothing():
    brain, backend, _ = _sim_brain()
    brain.start()
    brain.status()
    assert backend.writes == []
    brain.shutdown()


def test_status_after_start_reflects_the_controller_not_the_ini():
    cfg = Config()
    assert cfg.motion.voltage_x == 85 and cfg.motion.rate_x == 300   # the .ini defaults
    brain, _, events = _sim_brain(cfg)
    brain.start()
    st = brain.status()
    assert st.position_steps == FOUND["position"]
    assert st.voltage == [112.0] * 3
    assert st.step_rate == [500.0] * 3
    assert st.acceleration == [1000.0] * 3
    # adopted INTO the brain's config too: the px/step table row, Settings and
    # a remote GUI's get_config all follow the real voltage
    assert (cfg.motion.voltage_x, cfg.motion.rate_y, cfg.motion.acc_z) == (112, 500, 1000)
    # presets mirror what was found: 500 is nearer slow (300) than fast (1500),
    # 112 V nearer the max (125) than the min (85)
    assert st.speed_fast is False and st.step_large is True
    assert any("adopted from the controller" in m for _, m in events)
    brain.shutdown()


def test_out_of_window_value_is_reported_not_corrected():
    cfg = Config()
    cfg.limits.max_voltage = 110.0        # the controller holds 112 V
    brain, backend, events = _sim_brain(cfg)
    brain.start()
    assert brain.status().voltage[0] == 112.0
    assert backend.writes == []
    assert any(level == "warn" and "outside" in m for level, m in events)
    brain.shutdown()


def test_explicit_apply_config_still_writes():
    """The .ini values are still one click away -- they just need the click."""
    brain, backend, _ = _sim_brain()
    brain.start()
    brain.cfg.motion.voltage_x = 95.0
    brain.apply_config()
    assert "set_voltage" in backend.writes
    assert brain.status().voltage[0] == 95.0
    brain.shutdown()


def _fake_pylablib(monkeypatch, dev: FakeKim101):
    thorlabs = types.SimpleNamespace(KinesisPiezoMotor=lambda serial: dev,
                                     list_kinesis_devices=lambda: [("97000000", "KIM101")])
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    pkg = types.ModuleType("pylablib")
    pkg.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", pkg)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)


def test_real_backend_open_and_start_issue_no_state_changing_call(monkeypatch):
    dev = FakeKim101()        # holds 31/667/183 steps, 112 V / 500 / 1000
    dev.get_device_info = lambda: dev._op("get_device_info",
                                          types.SimpleNamespace(serial_no="97000000"))
    _fake_pylablib(monkeypatch, dev)
    cfg = Config()
    brain = Kim(KinesisKim(cfg), cfg)
    brain.start()
    st = brain.status()
    writes = [c for c in dev.calls if c not in READS]
    assert writes == [], f"start-up wrote to the KIM101: {writes}"
    assert st.position_steps == [31, 667, 183]
    assert st.voltage == [112.0] * 3 and st.step_rate == [500.0] * 3
    assert cfg.motion.voltage_z == 112
