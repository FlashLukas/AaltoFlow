"""An action's `wait.check`: "finished" is not "succeeded" (2026-09-24).

The camera's autofocus declares

    "wait": {"target_key": "af_id",
             "ready": {"policy": "adopt_then_flag", "setpoint_key": "af_id",
                       "flag_key": "af_running", "invert": true},
             "check": {"key": "af_error", "equals": "OK"}}

A killed or failed autofocus also ends with af_running False. Without the
check the routine would report success and the scan would measure out of
focus; with it the action raises and the hook's on_error decides.
"""

import pytest

from scan_core.instrument import InstrumentError
from scan_core.manifest import _action_from

WAIT = {"target_key": "af_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "af_id",
                  "flag_key": "af_running", "invert": True},
        "check": {"key": "af_error", "equals": "OK"},
        "timeout_s": 5}


class StubCamera:
    """Replies with a run number; the wait sees that run finished with `outcome`."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.sent = []

    def command(self, verb, **args):
        self.sent.append(verb)
        return {"ok": True, "af_id": 7}

    def wait_until(self, predicate, timeout_s=30.0, what=""):
        st = {"af_id": 7, "af_running": False, "af_error": self.outcome}
        assert predicate(st)                         # the wait targets run #7
        assert not predicate({**st, "af_id": 6})     # ...and no other
        return st


def _action(inst, wait=WAIT):
    d = {"id": "autofocus", "label": "Find focus", "kind": "action",
         "type": "action", "wait": wait}
    return _action_from(d, inst, "camera.autofocus", None)


def test_a_successful_run_returns():
    cam = StubCamera("OK")
    _action(cam).run()
    assert cam.sent == ["autofocus"]


@pytest.mark.parametrize("outcome", ["killed", "RuntimeError", "no Z"])
def test_a_failed_run_raises_with_what_the_module_said(outcome):
    with pytest.raises(InstrumentError, match=repr(outcome)):
        _action(StubCamera(outcome)).run()


def test_without_a_check_finishing_is_enough():
    wait = {k: v for k, v in WAIT.items() if k != "check"}
    _action(StubCamera("killed"), wait).run()      # old contract unchanged
