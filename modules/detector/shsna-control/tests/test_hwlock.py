"""shsna claims NO hardware: the analyser and its TG belong to the signalhound
service, which holds the hwlock claim (one physical address, one service).
The module still carries the suite's hwlock.py -- byte for byte the master
copy, which tools/check_modules.py insists on -- so it is checked here too."""

from pathlib import Path

import pytest

from shsna.config import Config
from shsna.sim_system import build_sim_system


def test_the_hwlock_copy_is_the_master_byte_for_byte():
    root = Path(__file__).resolve().parents[4]
    master = root / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed on its own)")
    import shsna.hwlock as mine
    assert Path(mine.__file__).read_bytes() == master.read_bytes()


def test_neither_backend_claims_anything(_private_lock_dir):
    from shsna.backends import real_backend
    cfg = Config()
    cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port = 17738, 17739   # nobody there
    sim, _ = build_sim_system(Config(), realtime=False)
    sim.start(run=False)
    real = real_backend(cfg)
    real.open()
    try:
        assert not _private_lock_dir.exists() or not any(_private_lock_dir.iterdir())
    finally:
        real.close()
        sim.shutdown()
