from clMag.ramp import Ramper


def test_large_move_steps_by_increment():
    r = Ramper(increment_A=0.05, delay_s=0.01)
    r.sync_to(0.0)
    r.go_to(0.2)
    assert abs(r.step() - 0.05) < 1e-12
    assert abs(r.step() - 0.10) < 1e-12
    assert abs(r.step() - 0.15) < 1e-12
    assert abs(r.step() - 0.20) < 1e-12
    assert r.done


def test_small_move_applied_directly():
    # a change smaller than one increment snaps straight to target in one step
    r = Ramper(increment_A=0.05, delay_s=0.01)
    r.sync_to(1.0)
    r.go_to(1.02)               # 0.02 < 0.05
    assert r.is_small_move
    assert abs(r.step() - 1.02) < 1e-12
    assert r.done


def test_negative_direction():
    r = Ramper(increment_A=0.05, delay_s=0.01)
    r.sync_to(0.0)
    r.go_to(-0.12)
    assert abs(r.step() - (-0.05)) < 1e-12
    assert abs(r.step() - (-0.10)) < 1e-12
    assert abs(r.step() - (-0.12)) < 1e-12   # remainder < increment -> snap
    assert r.done
