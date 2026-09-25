from zpiezo.config import Config, load_config, save_config


def test_ini_roundtrip(tmp_path):
    cfg = Config()
    cfg.limits.v_max = 60.0
    cfg.hardware.serial = "29250001"
    cfg.hardware.step_v = 0.5
    p = tmp_path / "z.ini"
    save_config(cfg, str(p))
    back = load_config(str(p))
    assert back.limits.v_max == 60.0
    assert back.hardware.serial == "29250001"
    assert back.hardware.step_v == 0.5


def test_bool_roundtrip(tmp_path):
    for value in (True, False):
        cfg = Config()
        cfg.limits.enforce = value
        p = tmp_path / f"z_{value}.ini"
        save_config(cfg, str(p))
        assert load_config(str(p)).limits.enforce is value
