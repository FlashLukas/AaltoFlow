"""scan-core's generic client speaks CurveZMQ to exactly the modules the lab's
policy secures (suite_common/secure.py), and plain to the rest.

No service is needed: the socket's security mechanism is set before it
connects. Port 18965 is only named, never reached.
"""

from __future__ import annotations

import json

import pytest

zmq = pytest.importorskip("zmq")

from suite_common import secure  # noqa: E402

from scan_core.instrument import Instrument  # noqa: E402


@pytest.fixture
def secured(tmp_path, monkeypatch):
    me, kr = tmp_path / "me", tmp_path / "keyring"
    me.mkdir()
    kr.mkdir()
    public, secret = secure.new_keypair()
    secure.write_cert(me / secure.OWN_PUBLIC, public, meta={"pc": "pc-a"})
    secure.write_cert(me / secure.OWN_SECRET, public, secret, meta={"pc": "pc-a"})
    (me / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}), encoding="utf-8")
    (kr / secure.POLICY_FILE).write_text(json.dumps({"mode": "enforce", "modules": ["kim"]}),
                                         encoding="utf-8")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(me))
    return public


@pytest.mark.parametrize("name, curve", [("kim", True), ("kim_pc-a", True),
                                         ("piezo", False)])
def test_the_instrument_uses_curve_only_where_the_policy_says(secured, name, curve):
    inst = Instrument(name, host="127.0.0.1", cmd_port=18965, timeout_ms=200)
    try:
        want = zmq.CURVE if curve else zmq.NULL
        assert inst._req.getsockopt(zmq.MECHANISM) == want
        assert inst._sub.getsockopt(zmq.MECHANISM) == want
        inst._reset_req()                      # the rebuilt socket, too
        assert inst._req.getsockopt(zmq.MECHANISM) == want
    finally:
        inst.close()


def test_a_secured_module_on_an_unknown_pc_says_what_is_missing(secured):
    with pytest.raises(secure.SecurityError, match="no key for '10.9.9.9'"):
        Instrument("kim", host="10.9.9.9", cmd_port=18965, timeout_ms=200)
