"""
preview.py — the setpoints ONE axis will actually send, before anything moves.

The axis row shows from / to / pts; what the instrument receives is one step
further along: `Recipe.compile` turns the row into a value vector, `Settable.set`
CLAMPS each value to the live limits, and `manifest.py` ROUNDS an int control.
A preview that skipped either of those would show numbers the scan never visits
-- so this reuses the engine's own compile step and repeats exactly those two
transformations, nothing else. No Qt: the dialog in apps/scan_builder.py only
draws what this returns, and the tests check it without a screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .recipe import _compile_axis


@dataclass
class MemberPreview:
    """One parameter's column: what the recipe asks for and what gets sent."""
    pid: str
    label: str
    unit: str
    requested: np.ndarray
    sent: np.ndarray
    #: per-point remark, "" when the value goes out unchanged
    notes: list[str] = field(default_factory=list)

    @property
    def n_changed(self) -> int:
        return sum(1 for n in self.notes if n)

    @property
    def n_repeats(self) -> int:
        """Points that send a value an earlier point of this axis already sent
        (rounding or clamping collapsed them) -- measured twice, not two places."""
        return len(self.sent) - len(np.unique(self.sent))


@dataclass
class DimPreview:
    """One dimension of the grid (a raster row gives two, a zip one with N members)."""
    name: str
    kind: str
    members: list[MemberPreview]

    @property
    def size(self) -> int:
        return len(self.members[0].sent) if self.members else 0


def _member(pid: str, values: np.ndarray, registry) -> MemberPreview:
    p = registry.get(pid) if registry is not None else None
    req = np.asarray(values, dtype=float)
    sent = req.copy()
    notes = [""] * len(req)
    if p is not None and getattr(p, "limits", None) is not None:
        lo, hi = p.limits
        # Settable.set clamps -- the safety envelope lives there, not in the UI
        clamped = np.clip(sent, lo, hi)
        for i in np.flatnonzero(clamped != sent):
            notes[i] = f"clamped from {req[i]:g}"
        sent = clamped
    if p is not None and getattr(p, "integer", False):
        # manifest.py sends int(round(value)) for an int control
        rounded = np.round(sent)
        for i in np.flatnonzero(rounded != sent):
            notes[i] = (notes[i] + "; " if notes[i] else "") + f"rounded from {sent[i]:g}"
        sent = rounded
    return MemberPreview(pid=pid,
                         label=getattr(p, "label", pid) if p is not None else pid,
                         unit=getattr(p, "unit", "") if p is not None else "",
                         requested=req, sent=sent, notes=notes)


def preview_axis(ax: dict, registry=None) -> list[DimPreview]:
    """The dims one recipe axis compiles to, with the values each point sends."""
    return [DimPreview(name=d.name, kind=d.kind,
                       members=[_member(pid, vals, registry) for pid, vals in d.params])
            for d in _compile_axis(ax)]


def step_summary(values: np.ndarray) -> str:
    """'step 1.5' for an even grid, 'step 0.9 .. 1.2 (uneven)' otherwise."""
    if len(values) < 2:
        return "single point"
    d = np.diff(np.asarray(values, dtype=float))
    lo, hi = float(d.min()), float(d.max())
    scale = max(abs(lo), abs(hi), 1e-300)
    if math.isclose(lo, hi, rel_tol=1e-9, abs_tol=1e-12 * scale):
        return f"step {lo:g}"
    return f"step {lo:g} .. {hi:g} (uneven)"
