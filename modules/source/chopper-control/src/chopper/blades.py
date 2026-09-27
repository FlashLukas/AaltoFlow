"""The chopper blades the MC2000B knows, and what each one allows.

Everything that depends on WHICH blade is mounted lives in this one table:

  * the index the controller uses for it (`blade=n`, manual section 8.2);
  * its slot count(s) -- a "two-frequency" blade has an OUTER and an INNER ring;
  * the chopping-frequency range of each ring (manual section 12, "Chopping
    Range"), which is why the frequency limits in `describe` change when the
    blade changes;
  * the reference-IN modes (`ref=n`, section 8.3) and reference-OUT modes
    (`output=n`, section 8.4) it offers, in the controller's index order;
  * the frequency resolution of the synthesiser for that blade family.

Only the two blades on hand are listed with full detail (MC1F10HP, shipped with
the MC2000B-EC, and MC1F60); the others are here so a blade reported by the
controller is at least NAMED, and they can be used by adding them to
`blades.owned` in the config. Their ranges come from the same manual table.

CONVENTION for a two-ring blade (the only thing in this file that is a
decision, not a copy of the manual): the frequency the user sets and reads is
the frequency of the ring the REFERENCE-IN mode locks to -- "int-inner" means
the inner ring chops at the set frequency, "int-outer" the outer ring. The
other ring then chops at `f * slots_other / slots_ref`. Whether the
controller's own `freq=` follows the same convention is # VERIFY on the unit
(see backends/mc2000b.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Blade:
    name: str                     # Thorlabs part number, e.g. "MC1F60"
    index: int                    # the controller's blade=n index
    outer_slots: int              # slots on the outer (or only) ring
    inner_slots: int              # 0 for a single-ring blade
    outer_range_Hz: tuple         # (min, max) chopping frequency of the outer ring
    inner_range_Hz: tuple         # (min, max) of the inner ring, () if none
    ref_modes: tuple              # reference-IN names, in ref=n index order
    output_modes: tuple           # reference-OUT names, in output=n index order
    resolution_Hz: float          # synthesiser step for this blade family

    @property
    def two_ring(self) -> bool:
        return self.inner_slots > 0

    def ring_of(self, ref_mode: str) -> str:
        """Which ring a reference-IN mode locks to: 'outer' or 'inner'.
        A single-ring blade only has 'outer'."""
        return "inner" if ref_mode.endswith("inner") else "outer"

    def is_external(self, ref_mode: str) -> bool:
        return ref_mode.startswith("ext")

    def slots(self, ring: str) -> int:
        return self.inner_slots if (ring == "inner" and self.two_ring) else self.outer_slots

    def range_Hz(self, ref_mode: str) -> tuple:
        """Allowed chopping frequency for the ring this reference mode locks to."""
        if self.ring_of(ref_mode) == "inner" and self.two_ring:
            return self.inner_range_Hz
        return self.outer_range_Hz

    def output_ring(self, output_mode: str, ref_mode: str) -> str | None:
        """Which ring the REF OUT signal follows, or None when it is the
        synthesiser ("target"), which says nothing about the real wheel."""
        if output_mode == "target":
            return None
        if output_mode in ("outer", "inner"):
            return output_mode
        # "actual" on a single blade: the sensor reads the outer (only) ring.
        # "sum"/"diff" of the MC2F blades are not modelled (not on hand).
        return "outer" if output_mode == "actual" else None


# Reference mode families (manual section 8.3 / 8.4).
_REF_SINGLE = ("internal", "external")
_REF_PRECISION = ("int-outer", "int-inner", "ext-outer", "ext-inner")
_OUT_SINGLE = ("target", "actual")
_OUT_PRECISION = ("target", "outer", "inner")
_OUT_TWO_FREQ = ("target", "outer", "inner", "sum", "diff")


def _single(name, index, slots, lo, hi, res=1.0):
    return Blade(name, index, slots, 0, (lo, hi), (), _REF_SINGLE, _OUT_SINGLE, res)


#: Every blade index 0..14 (manual section 8.2). Ranges from section 12.
BLADES = {b.name: b for b in (
    Blade("MC1F2", 0, 100, 2, (200.0, 10_000.0), (4.0, 200.0),
          _REF_PRECISION, _OUT_PRECISION, 0.01),
    _single("MC1F10", 1, 10, 20.0, 1_000.0),
    _single("MC1F15", 2, 15, 30.0, 1_500.0),
    _single("MC1F30", 3, 30, 60.0, 3_000.0),
    _single("MC1F60", 4, 60, 120.0, 6_000.0),
    _single("MC1F100", 5, 100, 200.0, 10_000.0),
    # The "higher precision" 10/100 blade: 100 outer slots lock the motor, the
    # 10 inner slots are what a beam usually goes through (manual section 6.5).
    Blade("MC1F10HP", 6, 100, 10, (200.0, 10_000.0), (20.0, 1_000.0),
          _REF_PRECISION, _OUT_PRECISION, 0.1),
    Blade("MC1F2P10", 7, 100, 2, (200.0, 10_000.0), (4.0, 200.0),
          _REF_PRECISION, _OUT_PRECISION, 0.01),
    _single("MC1F6P10", 8, 6, 12.0, 600.0),
    _single("MC1F10A", 9, 10, 20.0, 1_000.0),
    Blade("MC2F330", 10, 30, 3, (60.0, 3_000.0), (6.0, 300.0),
          _REF_SINGLE, _OUT_TWO_FREQ, 1.0),
    Blade("MC2F47", 11, 7, 4, (14.0, 700.0), (8.0, 400.0),
          _REF_SINGLE, _OUT_TWO_FREQ, 1.0),
    Blade("MC2F57B", 12, 7, 5, (14.0, 700.0), (10.0, 500.0),
          _REF_SINGLE, _OUT_TWO_FREQ, 1.0),
    Blade("MC2F860", 13, 60, 8, (120.0, 6_000.0), (16.0, 800.0),
          _REF_SINGLE, _OUT_TWO_FREQ, 1.0),
    Blade("MC2F5360", 14, 60, 53, (120.0, 6_000.0), (106.0, 5_300.0),
          _REF_SINGLE, _OUT_TWO_FREQ, 1.0),
)}

BY_INDEX = {b.index: b for b in BLADES.values()}


def blade_by_index(index: int) -> Blade:
    try:
        return BY_INDEX[int(index)]
    except KeyError:
        raise ValueError(f"unknown blade index {index!r} (0..14)") from None


def blade_by_name(name: str) -> Blade:
    key = str(name).strip().upper()
    for b in BLADES.values():
        if b.name.upper() == key:
            return b
    raise ValueError(f"unknown blade {name!r}; known: {', '.join(BLADES)}")


def parse_owned(text: str) -> list[str]:
    """'MC1F10HP, MC1F60' -> ['MC1F10HP', 'MC1F60'] (unknown names dropped)."""
    out = []
    for tok in str(text or "").replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            name = blade_by_name(tok).name
        except ValueError:
            continue
        if name not in out:
            out.append(name)
    return out
