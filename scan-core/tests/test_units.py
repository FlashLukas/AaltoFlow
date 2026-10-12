"""units.py: length detection and rate conversion (2026-10-12)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan_core.units import (is_length_unit, length_factor_um, rate_factor,  # noqa: E402
                             split_rate)


def test_case_insensitive_where_unique():
    from scan_core.units import family_of
    assert family_of("ghz") == ("frequency", 1e9)
    assert family_of("Mhz") == ("frequency", 1e6)
    assert family_of("MM") == ("length", 1e3)
    assert family_of("mt") == family_of("mT")
    assert family_of("mT") == ("field", 1e-3)            # exact first
    # single letters keep their case: m metre is not M, G gauss is not g
    assert family_of("M") is None and family_of("g") is None
    assert family_of("m") == ("length", 1e6)
    assert rate_factor("um", "um/MIN") == 60.0
    assert rate_factor("MHZ", "GHz/s") == pytest.approx(1e-3)
    assert split_rate("K/S") is None                      # S is siemens, not s


def test_lengths():
    for u in ("m", "mm", "um", "µm", "nm", " um "):
        assert is_length_unit(u), u
    for u in ("deg", "mT", "", None, "um/s", "steps"):
        assert not is_length_unit(u), u
    assert length_factor_um("mm") == 1000.0 and length_factor_um("nm") == 1e-3


def test_rates():
    assert split_rate("mT/min") == ("mT", 60.0)
    assert split_rate("mm") is None and split_rate("um/fortnight") is None
    assert rate_factor("um", "um/s") == 1.0
    assert rate_factor("um", "nm/min") == pytest.approx(60000.0)
    assert rate_factor("um", "mm/s") == pytest.approx(1e-3)
    assert rate_factor("mT", "mT/min") == 60.0
    assert rate_factor("mT", "T/h") == pytest.approx(3.6)
    assert rate_factor("K", "K/min") == 60.0
    assert rate_factor("deg", "deg/s") == 1.0
    assert rate_factor("steps", "steps/min") == 60.0     # unknown unit: only itself
    assert rate_factor("mT", "um/s") is None
    assert rate_factor("um", "um") is None               # not a rate
