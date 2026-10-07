"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `dssg` straight from src/, which is handy while iterating.
"""

import os
import sys
import tempfile

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="aaltoflow-nosec-")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))


import pytest  # noqa: E402


@pytest.fixture
def step_power(monkeypatch):
    """The power behaviour BEFORE fine power (2026-10-07): the attenuator's
    0.5 dB steps and the vernier as raw counts of its own. The tests written
    for that mode use this; test_fine_power.py tests the new default."""
    from dssg.synthesizer import Synthesizer
    monkeypatch.setattr(Synthesizer, "fine_power", lambda self: False)
