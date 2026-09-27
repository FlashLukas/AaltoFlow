"""The blade table: indices as in the manual (8.2), ranges as in its spec table."""

import pytest

from chopper.blades import (BLADES, BY_INDEX, blade_by_index, blade_by_name,
                            parse_owned)


def test_every_index_0_to_14_exactly_once():
    assert sorted(BY_INDEX) == list(range(15))
    assert len(BLADES) == 15


def test_the_two_blades_on_hand():
    b60 = blade_by_name("MC1F60")
    assert b60.index == 4 and b60.outer_slots == 60 and not b60.two_ring
    assert b60.range_Hz("internal") == (120.0, 6000.0)
    assert b60.ref_modes == ("internal", "external")
    assert b60.output_modes == ("target", "actual")

    hp = blade_by_name("mc1f10hp")                 # case-insensitive
    assert hp.index == 6 and hp.two_ring
    assert (hp.outer_slots, hp.inner_slots) == (100, 10)
    assert hp.range_Hz("int-inner") == (20.0, 1000.0)
    assert hp.range_Hz("int-outer") == (200.0, 10000.0)
    assert hp.ref_modes == ("int-outer", "int-inner", "ext-outer", "ext-inner")
    assert hp.output_modes == ("target", "outer", "inner")


def test_rings_and_output_rings():
    hp = blade_by_name("MC1F10HP")
    assert hp.ring_of("ext-inner") == "inner" and hp.is_external("ext-inner")
    assert hp.output_ring("target", "int-inner") is None
    assert hp.output_ring("outer", "int-inner") == "outer"
    b60 = blade_by_name("MC1F60")
    assert b60.output_ring("actual", "internal") == "outer"
    assert b60.slots("inner") == 60                # single ring: only one slot count


def test_lookups_and_owned_parsing():
    assert blade_by_index(4).name == "MC1F60"
    with pytest.raises(ValueError):
        blade_by_index(15)
    with pytest.raises(ValueError):
        blade_by_name("MC9")
    assert parse_owned("MC1F10HP, mc1f60 ; bogus, MC1F60") == ["MC1F10HP", "MC1F60"]
