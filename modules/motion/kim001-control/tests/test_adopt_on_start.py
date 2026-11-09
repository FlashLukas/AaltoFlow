"""Adopt-on-start (Lukas, 2026-09-27): starting the software must not change the stage.

Until then `Kim.start()` and `KinesisKim.open()` both PUSHED the .ini's drive
parameters (85 V / 300 steps/s / 5000 steps/s^2) onto every channel -- on the
lab unit that silently replaced the 112 V / 500 / 1000 someone had set in
Kinesis. Now start-up only READS, and the brain adopts what it finds.

The "controller" in these tests starts from a state the config would never
produce (the numbers kim-control found on the lab KIM101 on 2026-09-13), so a
brain that still pushed its config would be caught by the status, not only by
the write log. On a KIM001 only the focus axis (Z, index 2) is read; X / Y are
the "not fitted" zeros.
"""

from __future__ import annotations

import sys
import types

from kim001.backends.kinesis_kim001 import KinesisKim
from kim001.backends.sim import SimKim
from kim001.config import Config
from kim001.kim001 import Kim

from test_kinesis_backend import FakeKim001

# A controller state the .ini would never produce (kim-control's lab KIM101).
FOUND = {"position": 183, "rate": 500, "accel": 1000, "voltage": 112}

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
    assert cfg.motion.voltage_z == 85 and cfg.motion.rate_z == 300   # the .ini defaults
    brain, _, events = _sim_brain(cfg)
    brain.start()
    st = brain.status()
    assert st.position_steps == [0, 0, 183]
    assert st.voltage == [0.0, 0.0, 112.0]          # X / Y: not fitted
    assert st.step_rate == [0.0, 0.0, 500.0]
    assert st.acceleration == [0.0, 0.0, 1000.0]
    # adopted INTO the brain's config too: Settings and a remote GUI's
    # get_config follow the real drive values
    assert (cfg.motion.voltage_z, cfg.motion.rate_z, cfg.motion.acc_z) == (112, 500, 1000)
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
    assert brain.status().voltage[2] == 112.0
    assert backend.writes == []
    assert any(level == "warn" and "outside" in m for level, m in events)
    brain.shutdown()


def test_explicit_apply_config_still_writes():
    """The .ini values are still one click away -- they just need the click."""
    brain, backend, _ = _sim_brain()
    brain.start()
    brain.cfg.motion.voltage_z = 95.0
    brain.apply_config()
    assert "set_voltage" in backend.writes
    assert brain.status().voltage[2] == 95.0
    brain.shutdown()


def _fake_pylablib(monkeypatch, dev: FakeKim001):
    thorlabs = types.SimpleNamespace(KinesisPiezoMotor=lambda serial: dev,
                                     list_kinesis_devices=lambda: [("97000000", "")])
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    pkg = types.ModuleType("pylablib")
    pkg.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", pkg)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)


def test_real_backend_open_and_start_issue_no_state_changing_call(monkeypatch):
    dev = FakeKim001()        # holds 183 steps, 112 V / 500 / 1000 on channel 1
    _fake_pylablib(monkeypatch, dev)
    cfg = Config()
    brain = Kim(KinesisKim(cfg), cfg)
    brain.start()
    st = brain.status()
    writes = [c for c in dev.calls if c not in READS]
    assert writes == [], f"start-up wrote to the KIM001: {writes}"
    assert st.position_steps == [0, 0, 183]
    assert st.voltage == [0.0, 0.0, 112.0] and st.step_rate == [0.0, 0.0, 500.0]
    assert cfg.motion.voltage_z == 112
