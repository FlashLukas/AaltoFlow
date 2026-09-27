from zpiezo.config import Config
from zpiezo.sim_system import build_sim_system


def test_set_read_and_clamp():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.set_voltage(7.5) == 7.5
    assert brain.read_voltage() == 7.5
    # clamp both ends
    assert brain.set_voltage(999) == brain.cfg.limits.v_max
    assert brain.set_voltage(-5) == brain.cfg.limits.v_min
    brain.shutdown()


def test_status_snapshot_never_throws():
    brain, _ = build_sim_system(Config())
    brain.start()
    s = brain.status()
    assert s.connected and s.v_max == 75.0
    brain.shutdown()


def test_events_on_clamp():
    brain, _ = build_sim_system(Config())
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    brain.set_voltage(500)
    assert any(level == "warn" for level, _ in events)
    brain.shutdown()
