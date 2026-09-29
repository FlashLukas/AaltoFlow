"""One scan at a time per instrument (suite_common/control.py, claim_scan).

Lukas (2026-09-29): "make sure there is no more than one scanning core
running the same instruments". Two suites -- on the same PC or on two -- each
build their own Lab; the service lets only the scan that claimed it drive it.
Ports 15896-15899.
"""

from __future__ import annotations

import pytest

from conftest import CONTROLLED_MANIFEST, ControlledFake, FakeService
from scan_core.engine import run
from scan_core.instrument import ScanBusy
from scan_core.lab import build_lab_registry
from scan_core.recipe import Recipe


def _recipe(name="power line"):
    return Recipe(name=name, axes=[{"type": "linear", "param": "fake.rf_power",
                                    "start": -20, "stop": -10, "num": 3}],
                  detectors=["fake.measured_field"])


def _lab(port):
    return build_lab_registry(host="127.0.0.1", include=("clMag",),
                              ports={"clMag": port}, prefix=True)


@pytest.fixture
def svc():
    s = ControlledFake(15896, manifest=CONTROLLED_MANIFEST).start()
    yield s
    s.stop()


def test_a_second_scan_is_refused_before_it_moves_anything(svc):
    reg1, lab1 = _lab(svc.cmd_port)          # suite 1
    reg2, lab2 = _lab(svc.cmd_port)          # suite 2 (same PC or another)
    try:
        release = reg1.scan_claim({"fake.rf_power"}, "field map")
        assert svc.lease.status()["scan"]["label"] == "field map"
        n = len(svc.sent)
        with pytest.raises(ScanBusy, match="field map"):
            run(_recipe(), reg2)
        assert not any(m.get("cmd") == "set_power" for m in svc.sent[n:])
        release()
        ds = run(_recipe(), reg2)                    # free again: it runs
        assert ds["fake.measured_field"].shape == (3,)
        assert svc.lease.status()["scan"] is None    # and gives it back
    finally:
        lab1.close()
        lab2.close()


def test_the_claim_is_given_back_after_an_abort_and_after_an_error(svc, monkeypatch):
    reg, lab = _lab(svc.cmd_port)
    try:
        run(_recipe(), reg, should_abort=lambda: True)     # aborted: partial data
        assert svc.lease.status()["scan"] is None
        real = svc._handle
        monkeypatch.setattr(svc, "_handle", lambda m: {"ok": False, "error": "boom"}
                            if m.get("cmd") == "set_power" else real(m))
        with pytest.raises(Exception, match="boom"):
            run(_recipe(), reg)
        assert svc.lease.status()["scan"] is None
    finally:
        lab.close()


def test_a_service_that_predates_the_claim_still_scans_with_a_warning():
    old = FakeService(15898, manifest=CONTROLLED_MANIFEST).start()
    reg, lab = _lab(old.cmd_port)
    logs = []
    try:
        run(_recipe(), reg, on_log=logs.append)
        assert any("cannot be claimed" in m for m in logs)
    finally:
        lab.close()
        old.stop()
