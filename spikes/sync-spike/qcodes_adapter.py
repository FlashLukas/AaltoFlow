"""
qcodes_adapter.py
=================

The whole spike in one idea: wrap the ZeroMQ client as a QCoDeS `Instrument`
whose knobs are QCoDeS `Parameter`s.

Two parameters:
  - field  (settable)  -> its .set() does client.set_field_blocking(...):
                          it does NOT return until the field has SETTLED.
  - kerr   (gettable)  -> its .get() reads the detector.

Because `field.set()` blocks until settled, QCoDeS's sweep engine (`dond`)
automatically does exactly the logic you described:

    for each grid point:
        set every driven parameter   (blocks until each has settled)
        read every measured parameter
        store one row

Add a second settable parameter (say a second axis) and `dond` turns the same
loop into a 2-D grid -> a genuine multidimensional dataset, with no extra code
from you. That is the payoff of adopting a framework instead of hand-rolling
nested for-loops.
"""

from qcodes.instrument import Instrument
from qcodes.parameters import Parameter

from magnet_client import MagnetClient


class Magnet(Instrument):
    """A QCoDeS instrument backed by your ZeroMQ MagnetClient."""

    def __init__(self, name, port=5555, **kwargs):
        super().__init__(name, **kwargs)
        self._client = MagnetClient(port=port)

        # A DRIVEN parameter. set_cmd is a plain Python callable -> our blocking
        # client call. QCoDeS treats a call to field(value) as "set and wait".
        self.add_parameter(
            "field",
            unit="mT",
            label="Applied field",
            get_cmd=lambda: self._client.status()["field_mT"],
            set_cmd=self._set_field,
            parameter_class=Parameter,
        )

        # A MEASURED parameter. get_cmd reads the detector.
        self.add_parameter(
            "kerr",
            unit="a.u.",
            label="Kerr signal",
            get_cmd=self._client.read_signal,
            set_cmd=False,  # read-only
            parameter_class=Parameter,
        )

    def _set_field(self, value):
        # THIS is where "wait for the driven parameter to be set" lives.
        self._client.set_field_blocking(value)

    def get_idn(self):
        # QCoDeS asks every instrument to identify itself; fake it.
        return {"vendor": "spike", "model": "mock-magnet",
                "serial": "0001", "firmware": "0.1"}
