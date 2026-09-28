"""The scripts: the launcher's endpoints reach the real backend, and the
smoke test passes."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from shsna.config import Config

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_launcher_says_where_signalhound_listens(monkeypatch):
    run_service = _load("run_service")
    cfg = Config()
    monkeypatch.setenv("AALTOFLOW_ENDPOINTS", json.dumps(
        {"signalhound": ["localhost", 16001, 16002], "vna": ["x", 1, 2]}))
    run_service.apply_launcher_endpoints(cfg)
    hw = cfg.hardware
    assert (hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port) == ("127.0.0.1", 16001, 16002)


def test_no_endpoint_for_signalhound_leaves_the_config_alone(monkeypatch):
    run_service = _load("run_service")
    cfg = Config()
    monkeypatch.setenv("AALTOFLOW_ENDPOINTS", "not json")
    run_service.apply_launcher_endpoints(cfg)
    monkeypatch.setenv("AALTOFLOW_ENDPOINTS", json.dumps({"kim": ["h", 1, 2]}))
    run_service.apply_launcher_endpoints(cfg)
    assert cfg.hardware.owner_cmd_port == 5587 and cfg.hardware.owner_host == "127.0.0.1"


def test_the_console_speaks_the_raw_protocol_only():
    text = (SCRIPTS / "shsna_console.py").read_text(encoding="utf-8")
    assert "import shsna" not in text and "from shsna" not in text


def test_the_smoke_test_passes_and_prints_ascii():
    r = subprocess.run([sys.executable, str(SCRIPTS / "smoke_test.py")], capture_output=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    r.stdout.decode("ascii")                        # gotcha #14: a pipe is cp1252 at best
    assert b"smoke test passed" in r.stdout
