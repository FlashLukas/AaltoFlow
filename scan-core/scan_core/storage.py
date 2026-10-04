"""
storage.py -- how each recorded quantity is STORED in the data file (2026-10-04).

Until now every detector went into the file as an uncompressed 64-bit float,
whatever it was: a bool "overloaded" flag cost 8 bytes per point, a 12-bit
camera count the same, and a state name ("IDLE") could not be recorded at all.

The rule now: **the storage type comes from `describe`, not from a setting.**
Every module already declares the type of each value it publishes, so the
engine can choose the storage itself:

    declared            on disk                                  "not measured"
    --------            -------                                  --------------
    bool                uint8 0/1                                255
    int + min/max       narrowest integer that holds [min, max]  the type's extreme
                        (or "bits": N -> unsigned 0..2^N-1)      value OUTSIDE it
    int, no bounds      int32                                    -2147483648
    enum (options)      integer code 0..n-1 + CF flag_values /   -1
                        flag_meanings attributes
    string              netCDF-4 variable-length text            ""
    float               float64 ("store": "float32" -> float32)  NaN
    complex             <id>_real + <id>_imag, float64 (or 32)   NaN

IN MEMORY nothing changes for numbers. The engine's arrays stay float64 (or
complex128) with NaN = "not measured yet", so the live plots, the Measurement
tab, the fly-scan binning and the resonance window -- all of which do
arithmetic with NaN -- are untouched. The storage type is applied only when the
file is written, through xarray's CF encoding (`dtype` + `_FillValue` in each
variable's `.encoding`): xarray writes the fill value where the array has NaN,
and turns it back into NaN when the file is read. Only text is different in
memory: an object array of str, "" = not measured.

The declared range is a PROMISE. A value that does not fit -- an int outside
[min, max], a non-integer for an int, a bool that is not 0/1 -- STOPS the scan with a clear message (as a
trace whose length changed mid-scan does). It is never clipped and never
wrapped: a stored number that is silently wrong is worse than no number.
`to_memory` is that check; the engine calls it where it stores a measured value.

Every data variable is also compressed (zlib, lossless): a mostly-integer scan
shrinks a lot, a noisy float one a little; reading it back is transparent.
"""

from __future__ import annotations

import json
import math

import numpy as np

#: Lossless compression for every data variable (and the window's mask and
#: record variables). Level 4 is the usual sweet spot: most of the saving of
#: level 9 at a fraction of the time. `shuffle` regroups the bytes of each value
#: before compressing, which is what makes integer and float data compress well.
COMPRESSION = {"zlib": True, "complevel": 4, "shuffle": True}

#: the declared types a reader finds in each variable's `aaltoflow_type` attr
KINDS = ("float", "complex", "bool", "int", "enum", "string")

_INT32 = (-2**31, 2**31 - 1)


class StorageError(ValueError):
    """A measured value does not fit what its module declared. Stops the scan."""


def _int_candidates(unsigned_only: bool = False):
    """Integer types from narrow to wide; at each width signed first, so an
    unbounded or negative-going quantity lands in a familiar int16/int32.
    A `bits` quantity (a camera's 12-bit count) takes unsigned types only."""
    for size in (1, 2, 4, 8):
        if not unsigned_only:
            yield np.dtype(f"i{size}")
        yield np.dtype(f"u{size}")


def choose_int(lo: int, hi: int, unsigned_only: bool = False) -> tuple[np.dtype, int] | None:
    """The narrowest integer dtype holding [lo, hi] that still has a SPARE
    extreme value (outside [lo, hi]) to mean "not measured". None if no
    64-bit type does."""
    for dt in _int_candidates(unsigned_only):
        info = np.iinfo(dt)
        if lo < info.min or hi > info.max:
            continue
        # the fill must be a value the module promised never to report
        if dt.kind == "i" and lo > info.min:
            return dt, int(info.min)
        if hi < info.max:
            return dt, int(info.max)
        if dt.kind == "u" and lo > 0:
            return dt, 0
    return None


class Storage:
    """How one quantity is stored. Built from a describe descriptor
    (`from_descriptor`) or directly (the simulator, tests).

    kind    : one of KINDS
    lo, hi  : the declared range of an int (None = not declared)
    bits    : an int's declared bit depth (unsigned 0 .. 2^bits - 1)
    options : an enum's options, in order (code i = options[i])
    store   : "float32" to halve a float/complex detector, else None
    """

    def __init__(self, kind: str = "float", lo=None, hi=None, bits=None,
                 options=None, store=None):
        if kind not in KINDS:
            raise ValueError(f"unknown storage kind {kind!r}")
        self.kind = kind
        self.bits = int(bits) if bits is not None else None
        self.options = list(options or [])
        #: enum values met that are not options (the engine reports and empties it)
        self.unknown_seen: list = []
        self.store = store if store in ("float32", "float64") else None
        if kind == "int" and self.bits is not None:
            lo, hi = 0, 2 ** self.bits - 1
        # an infinite bound is the same as none (a module's "unbounded")
        lo = None if lo is None or not math.isfinite(float(lo)) else lo
        hi = None if hi is None or not math.isfinite(float(hi)) else hi
        self.lo = None if lo is None else _as_int_bound(lo, math.ceil)
        self.hi = None if hi is None else _as_int_bound(hi, math.floor)
        self.disk, self.fill = self._choose()

    # ---- construction ------------------------------------------------------
    @classmethod
    def from_descriptor(cls, d: dict, use_bounds: bool = True) -> "Storage":
        """The storage a describe descriptor implies.

        `use_bounds=False` for a CONTROL recorded as a detector: its min/max
        are the limits of what may be SET, not a promise about what the
        readback reports (a position control clamps setpoints; the encoder
        may still read a little beyond them). An int control is then stored
        as int32 (or by its `bits`), never narrower.
        """
        t = d.get("type")
        dt = d.get("dtype")
        store = d.get("store")
        if dt == "complex":
            return cls("complex", store=store)
        if dt == "float" and t != "int":     # an array declared as floats
            return cls("float", store=store)
        if t in ("bool", "enum", "string"):
            return cls(t, options=d.get("options"))
        if t == "int" or dt == "int":
            lo = d.get("min") if use_bounds else None
            hi = d.get("max") if use_bounds else None
            return cls("int", lo=lo, hi=hi, bits=d.get("bits"))
        return cls("float", store=store)

    @classmethod
    def from_dtype(cls, dtype: str) -> "Storage":
        """The storage of a Gettable built without one (the old `dtype` field):
        complex stays complex, int is an unbounded int, all else float."""
        if dtype == "complex":
            return cls("complex")
        if dtype == "int":
            return cls("int")
        if dtype in ("bool", "enum", "string"):
            return cls(dtype)
        return cls("float")

    def _choose(self):
        if self.kind in ("float", "complex"):
            return np.dtype(self.store or "float64"), None
        if self.kind == "bool":
            return np.dtype("uint8"), 255
        if self.kind == "enum":
            n = len(self.options)
            return np.dtype("int8" if n <= 127 else "int16" if n <= 32767
                            else "int32"), -1
        if self.kind == "string":
            return np.dtype(object), None
        # int: missing bounds are taken as int32's (a module that declares
        # none has promised nothing narrower), keeping int32's minimum spare
        lo = _INT32[0] + 1 if self.lo is None else self.lo
        hi = _INT32[1] if self.hi is None else self.hi
        if lo > hi:
            raise ValueError(f"int range [{lo}, {hi}] is empty")
        got = choose_int(lo, hi, unsigned_only=self.bits is not None)
        if got is None:                 # beyond 64 bits: keep it as a float
            return np.dtype("float64"), None
        return got

    # ---- range: what the module promised ------------------------------------
    @property
    def int_range(self) -> tuple[int, int]:
        lo = _INT32[0] + 1 if self.lo is None else self.lo
        hi = _INT32[1] if self.hi is None else self.hi
        return lo, hi

    # ---- the in-memory array -----------------------------------------------
    def allocate(self, shape) -> np.ndarray:
        """The engine's buffer, every point "not measured"."""
        if self.kind == "complex":
            return np.full(shape, np.nan + 1j * np.nan, dtype=np.complex128)
        if self.kind == "string":
            return np.full(shape, "", dtype=object)
        return np.full(shape, np.nan, dtype=float)

    # ---- one measured value -> the buffer, CHECKED ------------------------
    def to_memory(self, value, what: str = ""):
        """A measured value as the engine stores it in memory -- or StorageError.

        float/complex: unchanged (NaN is "not measured", as always).
        bool / int: a float holding the exact value, after the range check.
        enum: the option's index as a float. string: str ("" = nothing).
        None / NaN mean "no value at this point" for every kind -- the same as
        for a float detector whose read came back empty -- and are stored as
        "not measured", not refused.
        Works element-wise on an ARRAY value (a trace of ints, say).
        """
        k = self.kind
        if k in ("float", "complex"):
            return value
        if k == "string":
            if isinstance(value, (list, tuple, np.ndarray)):
                return np.array([_text(v) for v in np.ravel(np.asarray(value, dtype=object))],
                                dtype=object).reshape(np.shape(value))
            return _text(value)
        if isinstance(value, (list, tuple, np.ndarray)):
            arr = np.asarray(value, dtype=object)
            flat = [self._scalar(v, what) for v in arr.ravel()]
            return np.array(flat, dtype=float).reshape(arr.shape)
        return self._scalar(value, what)

    def _scalar(self, v, what):
        if v is None or (isinstance(v, (float, np.floating)) and math.isnan(v)):
            return math.nan
        if self.kind == "enum":
            code = self._code(v, what)
            return math.nan if code < 0 else float(code)
        # bool and int need a NUMBER; a string "5" is a module bug, not a 5
        if isinstance(v, (str, bytes)) or not isinstance(v, (int, float, np.number, np.bool_)):
            raise StorageError(f"{what}: got {v!r}, which is not a number, but its "
                               f"module declares it as {self.kind}")
        x = float(v)
        if self.kind == "bool":
            if x not in (0.0, 1.0):
                raise StorageError(f"{what}: got {v!r}, but its module declares it "
                                   f"as bool (only True/False, 1/0 can be stored)")
            return x
        # int
        if not math.isfinite(x) or x != math.floor(x):
            raise StorageError(f"{what}: got {v!r}, which is not a whole number, "
                               f"but its module declares it as int")
        lo, hi = self.int_range
        if not (lo <= x <= hi):
            where = (f"its declared range [{self.lo}, {self.hi}]"
                     if (self.lo is not None or self.hi is not None) else
                     f"int32 (no min/max declared)")
            if self.bits is not None:
                where = f"{self.bits} bits [0, {hi}]"
            raise StorageError(f"{what}: got {int(x)}, outside {where}. The value is "
                               f"not clipped; the module's describe must declare "
                               f"the range the instrument really reports")
        return x

    def _code(self, v, what) -> int:
        for i, opt in enumerate(self.options):
            if v == opt:
                return i
        for i, opt in enumerate(self.options):    # 3 vs "3", a JSON round trip
            if str(v) == str(opt):
                return i
        # NOT a stop (Lukas, 2026-10-04): several modules read back a value
        # outside their own option list ("--" for an unknown sensitivity, a
        # front-panel time constant the list does not offer). The point is
        # stored as "not measured" and the engine logs it once (unknown_seen).
        # A wrong NUMBER still stops the scan: that would be wrong data.
        self.unknown_seen.append(v)
        return -1

    # ---- the file -----------------------------------------------------------
    def attrs(self) -> dict:
        """Attributes that say what the variable IS, for any reader."""
        out = {"aaltoflow_type": self.kind}
        if self.kind == "int":
            if self.lo is not None:
                out["declared_min"] = int(self.lo)
            if self.hi is not None:
                out["declared_max"] = int(self.hi)
            if self.bits is not None:
                out["declared_bits"] = int(self.bits)
        if self.kind == "enum":
            # CF: flag_values + flag_meanings (blank-separated words, so a
            # blank inside an option becomes "_"); the exact original list
            # rides along as JSON for a reader that wants it back verbatim
            out["flag_values"] = np.arange(len(self.options), dtype=self.disk)
            out["flag_meanings"] = " ".join(str(o).replace(" ", "_") or "_"
                                            for o in self.options)
            out["options_json"] = json.dumps(self.options)
        if self.kind == "bool":
            out["flag_values"] = np.array([0, 1], dtype="uint8")
            out["flag_meanings"] = "false true"
        return out

    def encoding(self) -> dict:
        """xarray encoding for this variable (dtype, fill, compression)."""
        if self.kind == "string":
            # variable-length text: no fill value ("" is "not measured") and
            # no compression filter (HDF5 cannot usefully compress the
            # pointers a variable-length string dataset holds)
            return {"dtype": str}
        enc = {"dtype": self.disk, **COMPRESSION}
        if self.fill is not None:
            enc["_FillValue"] = self.disk.type(self.fill)
        return enc


def storage_of(param) -> Storage:
    """A registry parameter's storage; FLOAT for one that declares nothing."""
    st = getattr(param, "storage", None)
    if isinstance(st, Storage):
        return st
    dtype = getattr(param, "dtype", None)
    if dtype:
        return Storage.from_dtype(dtype)
    return FLOAT


def _as_int_bound(v, rnd) -> int:
    x = float(v)
    if not math.isfinite(x):
        raise ValueError(f"int bound {v!r} is not finite")
    return int(rnd(x))


def _text(v) -> str:
    if v is None or (isinstance(v, (float, np.floating)) and math.isnan(v)):
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


#: the storage of a quantity nobody declared (the old behaviour, compressed)
FLOAT = Storage("float")

#: a fly scan's samples-per-pixel: a count, never negative; "row not flown"
#: is the uint32 maximum
COUNT = Storage("int", lo=0, hi=2**32 - 2)
