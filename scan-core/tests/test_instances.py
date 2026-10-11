"""Two copies of ONE module (a module INSTANCE, 2026-10-11) in one registry.

Mission Control can run a module twice on one PC -- the scope for the Siglent
and, as instance "dwf", for an Analog Discovery 2. Discovery gives the copy
its own slug ("scope_dwf"), and the scan engine must register both under
their own prefixes (gotcha #20) so one scan can use both.

Here: a throwaway suite root with one module "fake" plus its instance "b",
each answered by a fake service on scratch ports.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("zmq")
from conftest import DEMO_MANIFEST, FakeService      # noqa: E402

from scan_core import api                            # noqa: E402
from scan_core.lab import build_lab_registry, known_ports      # noqa: E402
from suite_common import modules as M                # noqa: E402

PORT_A, PORT_B = 17950, 17952


@pytest.fixture
def suite(tmp_path):
    d = tmp_path / "suite" / "modules" / "other" / "fake-control"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    (d / "module.toml").write_text(
        f'[module]\nkey = "fake"\nname = "Fake"\n[ports]\ncmd = {PORT_A}\n'
        f'[run]\nservice = "scripts/run_service.py"\n', encoding="utf-8")
    root = tmp_path / "suite"
    M.add_instance("fake", "b", PORT_B, args=["--driver", "dwf"], root=root)
    a = FakeService(PORT_A, adopt_delay=0.02, settle_delay=0.02,
                    manifest=DEMO_MANIFEST).start()
    b = FakeService(PORT_B, adopt_delay=0.02, settle_delay=0.02,
                    manifest=DEMO_MANIFEST).start()
    yield root, a, b
    a.stop()
    b.stop()


def test_follow_the_launcher_finds_both_copies_by_slug(suite):
    root, a, b = suite
    eps = api._discover_endpoints(None, None, root, lambda *_: None)
    assert eps == {"fake": ("localhost", PORT_A, PORT_A + 1),
                   "fake_b": ("localhost", PORT_B, PORT_B + 1)}
    # the module's own port table is NOT overwritten by its instance
    assert known_ports(root)["fake"] == (PORT_A, PORT_A + 1)
    # by name too: the id, the slug, or the key (= the module itself)
    assert set(api._discover_endpoints(None, ["fake#b"], root, print)) == {"fake_b"}
    assert set(api._discover_endpoints(None, ["fake_b", "fake"], root, print)) == \
        {"fake", "fake_b"}


def test_two_copies_of_one_module_get_two_prefixes_and_work_in_one_scan(suite, tmp_path):
    root, a, b = suite
    eps = api._discover_endpoints(None, None, root, lambda *_: None)
    reg, lab = build_lab_registry(include=tuple(eps), endpoints=eps, prefix=True)
    try:
        ids = {p.id for p in reg.settables()}
        assert {"fake.field", "fake_b.field"} <= ids
        gets = {g.id for g in reg.gettables()}
        assert {"fake.measured_field", "fake_b.measured_field"} <= gets
        # each copy's parameters reach ITS service, nobody else's
        reg.get("fake_b.field").set(12.5)
        reg.get("fake.field").set(-3.0)
        assert b._setpoint == 12.5 and a._setpoint == -3.0
        assert reg.owner["fake_b.field"] == "fake_b" and reg.owner["fake.field"] == "fake"
    finally:
        lab.close()

    # and both in ONE scan, through the scripting API
    with api.connect(root=root, data_dir=tmp_path / "data", log=None) as lab2:
        ds = lab2.scan({"name": "two copies",
                        "axes": [{"type": "linear", "param": "fake.field",
                                  "start": 0, "stop": 10, "num": 2},
                                 {"type": "linear", "param": "fake_b.field",
                                  "start": 20, "stop": 30, "num": 2}],
                        "detectors": ["fake.measured_field", "fake_b.measured_field"]},
                       save=False)
        assert ds["fake.measured_field"].shape == (2, 2)
        assert np.allclose(ds["fake_b.measured_field"].values[:, -1], 30, atol=1.0)
        assert np.allclose(ds["fake.measured_field"].values[-1, :], 10, atol=1.0)
