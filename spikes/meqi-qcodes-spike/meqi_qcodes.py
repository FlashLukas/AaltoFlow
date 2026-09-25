"""
meqi_qcodes.py
==============

Wrap your REAL MeqiClient (meqi/net/client.py, unchanged) as a QCoDeS instrument.

This is the whole adapter -- the only meqi-specific code QCoDeS needs. Note what
it does NOT do: it does not reimplement seeking, PID, calibration, or the state
machine. Those stay in your controller/service, exactly where they are. QCoDeS
only ever sees "set a field (blocks until stable)" and "read a number".

The one thing your client is missing today is a *blocking* set. `set_field` is
fire-and-forget; the settle signal lives in `status().field_stable`. So the
adapter adds a small `_wait_stable()` that polls status until the service reports
the new setpoint is adopted AND the field is stable -- the same loop your
scripts/client_demo.py already uses by hand. In the real project you'd move this
one method onto MeqiClient (e.g. `set_field_blocking`) and the adapter shrinks
further.
"""

import time

from qcodes.instrument import Instrument
from qcodes.parameters import Parameter

from meqi.net.client import MeqiClient


class Meqi(Instrument):
    def __init__(self, name, host="localhost", cmd_port=5555, pub_port=5556,
                 settle_timeout_s=20.0, **kwargs):
        super().__init__(name, **kwargs)
        self._settle_timeout = settle_timeout_s
        self._client = MeqiClient(host=host, cmd_port=cmd_port, pub_port=pub_port)
        self._client.start()  # fetch info + config, build calibration facade

        # DRIVEN parameter: setting it blocks until the field is stable.
        self.add_parameter(
            "field", unit="mT", label="Applied field",
            get_cmd=lambda: self._client.status().setpoint_field_mT,
            set_cmd=self._set_field_blocking,
            parameter_class=Parameter,
        )
        # MEASURED parameters: read straight off the status frame.
        self.add_parameter(
            "measured_field", unit="mT", label="Measured field",
            get_cmd=lambda: self._client.status().measured_field_mT,
            set_cmd=False, parameter_class=Parameter,
        )
        self.add_parameter(
            "current", unit="A", label="Coil current",
            get_cmd=lambda: self._client.status().current_A,
            set_cmd=False, parameter_class=Parameter,
        )

    def _set_field_blocking(self, target_mT):
        """Issue the fire-and-forget command, then wait for the real settle
        signal from the service. THIS is 'wait until the driven parameter is set'."""
        self._client.set_field(target_mT)   # fire-and-forget
        deadline = time.monotonic() + self._settle_timeout
        while time.monotonic() < deadline:
            s = self._client.status()
            adopted = (s.setpoint_field_mT is not None
                       and abs(s.setpoint_field_mT - target_mT) < 1e-6)
            if adopted and s.field_stable:   # service confirms new setpoint + stable
                return
            time.sleep(0.05)
        raise TimeoutError(f"field did not stabilise at {target_mT} mT "
                           f"in {self._settle_timeout}s")

    def get_idn(self):
        info = self._client.info()
        return {"vendor": "meqi", "model": "sim-magnet",
                "serial": "sim", "firmware": str(info.get("n_points", "?"))}

    def close(self):
        try:
            self._client.shutdown()
        finally:
            super().close()
