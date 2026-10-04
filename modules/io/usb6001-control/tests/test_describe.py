"""describe: the manifest follows the configuration the service STARTED with."""

import math

from usb6001.config import Config
from usb6001.net.describe import build_manifest, manifest_revision, read_path
from usb6001.sim_system import build_sim_system, demo_config


def _manifest(cfg, start=True):
    daq, _ = build_sim_system(cfg)
    if start:
        daq.start(poll=False)
    m = build_manifest(daq)
    return daq, m, {p["id"]: p for p in m["parameters"]}


def test_ids_follow_the_layout():
    daq, m, by = _manifest(demo_config())
    assert m["module"] == "usb6001"
    assert {"ao0", "ao1"} <= set(by)
    assert {f"do_p0_{i}" for i in range(4, 8)} <= set(by)          # outputs: controls
    assert {"di_p0_0", "di_p0_3", "di_p2_0"} <= set(by)            # inputs: detectors
    assert "do_p0_0" not in by and "di_p0_4" not in by
    assert not any("p1_" in k for k in by)                         # unused: nothing
    assert {"ai0", "ai3", "ai0_live"} <= set(by) and "ai5" not in by
    assert "ai2_V" in by and "ai0_V" not in by                     # only scaled ones
    daq.shutdown()


def test_changing_a_direction_changes_the_manifest_after_restart():
    cfg = demo_config()
    daq, m1, by1 = _manifest(cfg)
    cfg.dio.lines[0].direction = "out"
    # edited, not restarted: the manifest still describes the RUNNING tasks
    assert build_manifest(daq)["revision"] == m1["revision"]
    daq.shutdown()
    daq2, m2, by2 = _manifest(cfg)                                 # the "restart"
    assert "do_p0_0" in by2 and "di_p0_0" not in by2
    assert m2["revision"] != m1["revision"]
    daq2.shutdown()


def test_ao_bounds_follow_cfg_and_revision_ignores_values():
    cfg = demo_config()
    daq, m, by = _manifest(cfg)
    assert (by["ao1"]["min"], by["ao1"]["max"]) == (0.0, 5.0)
    daq.set_ao(0, 1.0)                                             # a value change
    assert build_manifest(daq)["revision"] == m["revision"]
    cfg.ao.channels[1].max_V = 3.0                                 # a bound change
    m2 = build_manifest(daq)
    assert {p["id"]: p for p in m2["parameters"]}["ao1"]["max"] == 3.0
    assert m2["revision"] != m["revision"]
    daq.shutdown()


def test_names_are_labels_ids_stay():
    cfg = demo_config()
    daq, m, by = _manifest(cfg)
    assert by["ai2"]["label"] == "Hall probe" and by["ai2"]["unit"] == "mT"
    assert by["do_p0_4"]["label"] == "Shutter"
    daq.shutdown()


def test_contract_shape():
    daq, m, by = _manifest(demo_config())
    daq.set_ao(0, 0.5)
    daq.poll_once()
    st = __import__("usb6001.net.protocol", fromlist=["x"]).status_to_dict(daq.status())
    for p in m["parameters"]:
        if p["kind"] == "control":
            assert p.get("set") and p.get("settle"), p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]
    # controls resolve against the live status
    assert read_path(st, by["ao0"]["read_path"]) == 0.5
    assert by["ao0"]["settle"] == {"policy": "echoes", "key": "ao_V", "index": 0, "tol": 1e-9}
    assert by["do_p0_4"]["set"]["extra"] == {"line": "p0.4"}
    # AI and DI detectors share ONE fresh acquisition keyed on the trigger reply
    acq = by["ai0"]["acquire"]
    assert acq["trigger_verb"] == "acquire" and acq["target_key"] == "acq_id"
    assert by["di_p0_0"]["acquire"] == acq
    assert by["ai0"]["read_path"] == ["sample", "ai", 0]
    # the acquire action is usable in scan routines
    assert by["acquire"]["wait"]["target_key"] == "acq_id"
    assert m["revision"] == manifest_revision(m)
    daq.shutdown()


def test_an_unstarted_brain_describes_its_config():
    _, m, by = _manifest(Config(), start=False)
    assert "ai0" in by and "di_p0_0" in by and not any(k.startswith("do_") for k in by)
    for p in m["parameters"]:
        for k in ("min", "max"):
            if k in p:
                assert math.isfinite(p[k])


# ---- declared types (scan-core STORES each detector in its declared type) ----

def _type_problem(d, v):
    """Why status value `v` does not fit descriptor `d` (the promise scan-core's
    storage keeps, developer notes 4b), or "". None = not measured, always fits."""
    if v is None:
        return ""
    t = d["type"]
    for x in (v if isinstance(v, list) else [v]):
        if x is None:
            continue
        if t == "bool" and not isinstance(x, bool):
            return f"bool reads {x!r}"
        if t == "int":
            if isinstance(x, bool) or x != int(x):
                return f"int reads {x!r}"
            if d["kind"] == "indicator" and (x < d.get("min", x) or x > d.get("max", x)):
                return f"{x!r} outside [{d.get('min')}, {d.get('max')}]"
        if t == "enum" and x not in d["options"]:
            return f"{x!r} not in {d['options']}"
        if t == "string" and not isinstance(x, str):
            return f"string reads {x!r}"
        if t == "float" and (isinstance(x, (bool, str)) or not isinstance(x, (int, float))):
            return f"float reads {x!r}"
    return ""


def _misfits(daq):
    from usb6001.net.protocol import status_to_dict
    st = status_to_dict(daq.status())
    return [f"{p['id']}: {_type_problem(p, read_path(st, p['read_path']))}"
            for p in build_manifest(daq)["parameters"] if p.get("read_path")
            and _type_problem(p, read_path(st, p["read_path"]))]


def test_declared_types_and_status_fits_before_and_after_a_sample():
    """Digital lines are bool (None = not known yet: the 6001 cannot read an
    output back before it is set), the acquisition counter an int >= 0."""
    daq, m, by = _manifest(demo_config())
    try:
        assert by["acq_id"]["type"] == "int" and by["acq_id"]["min"] == 0
        for k, p in by.items():
            if k.startswith(("do_", "di_")):
                assert p["type"] == "bool", k
        assert "store" not in str(m)
        assert _misfits(daq) == []            # before anything was read: None
        daq.fresh_sample(timeout_s=5.0)
        assert daq.status().acq_id == 1
        assert _misfits(daq) == []
    finally:
        daq.shutdown()
