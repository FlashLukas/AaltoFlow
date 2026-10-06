"""shutdown{keep_outputs} (Lukas, 2026-10-06).

The ppms service never changes the cryostat on a stop (field and temperature
are left as they are), so the restart flag changes nothing here: it is
accepted, the reply says the outputs were kept, and both kinds of shutdown send no command to MultiVu.
"""

import time

import pytest

pytest.importorskip("zmq")

from ppms.config import Config
from ppms.net.service import PpmsService
from ppms.sim_system import build_sim_system

CMD, PUB = 26140, 26141          # a port block no other ppms test uses


def _service():
    cryo, sim = build_sim_system(Config())
    svc = PpmsService(cryo, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    time.sleep(0.2)
    sim.writes = []
    for name in ("set_field", "set_temperature"):
        real = getattr(sim, name)

        def spy(*a, _real=real, _name=name, **kw):
            sim.writes.append(_name)
            return _real(*a, **kw)
        setattr(sim, name, spy)
    return svc, sim


@pytest.mark.parametrize("keep", [True, None, "false"])
def test_shutdown_never_touches_the_cryostat(keep):
    svc, sim = _service()
    msg = {"cmd": "shutdown"}
    if keep is not None:
        msg["keep_outputs"] = keep
    r = svc._dispatch(msg)
    # nothing is ever switched off here, so the outputs are always kept
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    assert svc._keep_outputs is (keep is True)     # "false" parsed as False
    svc.stop()
    assert sim.writes == [] and sim._open is False
