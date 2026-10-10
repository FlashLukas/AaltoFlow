"""Camera images as a scan detector (2026-10-10, roadmap phase 1 + the cheap
half of phase 2).

An IMAGE detector is an array detector with two inner axes: one frame per
scan point. These tests drive it in-process (a toy 12-bit camera), so they
cover the engine and the file, not the wire (test_image_over_the_wire.py):

* the frames are stored as uint16 (12 bit declared), compressed, one frame
  per chunk, with a per-point mask `<det>_measured`;
* unmeasured points read back as NaN (the fill value), measured ones exactly;
* above the size limit the frames are written into the data file AS THEY
  COME (framestore.FrameBuffer) -- checked here at a lowered limit -- and a
  scan that cannot do that (no data file, an averaged repeat) is refused with
  the size in the message;
* the size estimate the Scan tab shows;
* AaltoView's reduction can slice the result (no AaltoView change).
"""

from __future__ import annotations

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")
xr = pytest.importorskip("xarray")

from scan_core import framestore as FS                              # noqa: E402
from scan_core.autosave import write_dataset                        # noqa: E402
from scan_core.engine import run                                    # noqa: E402
from scan_core.errors import ScanAborted                            # noqa: E402
from scan_core.recipe import Recipe                                 # noqa: E402
from scan_core.registry import AxisSpec, Gettable, Registry, Settable  # noqa: E402
from scan_core.storage import Storage, StorageError                 # noqa: E402
from scan_core.view import Slice, reduce_cube                       # noqa: E402

H, W = 6, 8                                   # a tiny "sensor"


def _frame_for(x: float) -> np.ndarray:
    """A 12-bit frame that says which point it was taken at: pixel (r, c)
    = 100 * x + 10 * r + c, plus the full-scale corner 4095."""
    r, c = np.mgrid[0:H, 0:W]
    f = (100 * x + 10 * r + c).astype(np.uint16)
    f[0, 0] = 4095
    return f


def _camera_registry(bits=12):
    """X (a stage) and `cam.image`, a 12-bit frame per point."""
    state = {"x": 0.0, "grabs": 0}
    reg = Registry()
    reg.add(Settable("x", "X", "um", (0, 40),
                     set_fn=lambda v: state.__setitem__("x", v),
                     get_fn=lambda: state["x"]))

    def grab():
        state["grabs"] += 1
        return _frame_for(state["x"])
    axes = [AxisSpec("cam.image_y", "image y", "px", length=H,
                     values_fn=lambda: np.arange(H, dtype=float),
                     attrs={"um_per_px": 0.5}),
            AxisSpec("cam.image_x", "image x", "px", length=W,
                     values_fn=lambda: np.arange(W, dtype=float),
                     attrs={"um_per_px": 0.5})]
    axes[1].aux = {"cam.image_x_um": (list(np.arange(W) * 0.5), "um")}
    reg.add(Gettable("cam.image", "Camera image", "counts", grab, axes=axes,
                     dtype="int", storage=Storage("int", bits=bits)))
    return reg, state


def _recipe(n=5, **kw):
    return Recipe(name="img", axes=[{"type": "linear", "param": "x",
                                     "start": 0, "stop": n - 1, "num": n}],
                  detectors=["cam.image"], **kw)


# ───────────────────────── in memory (small maps) ────────────────────────────

def test_a_small_map_is_uint16_compressed_one_frame_per_chunk(tmp_path):
    reg, state = _camera_registry()
    ds = run(_recipe(), reg, created_iso="t")
    da = ds["cam.image"]
    assert da.dims == ("x", "cam.image_y", "cam.image_x")
    # in memory: float32 (exact for 12 bit), NaN = not measured
    assert da.dtype == np.float32
    assert np.array_equal(da.values[3], _frame_for(3.0).astype(np.float32))
    enc = da.encoding
    assert np.dtype(enc["dtype"]) == np.uint16 and enc["_FillValue"] == 65535
    assert enc["zlib"] and enc["chunksizes"] == (1, H, W)
    assert da.attrs[FS.IMAGE_ATTR] == 1 and da.attrs["declared_bits"] == 12
    assert ds["cam.image_measured"].values.all()
    # the pixel axes carry their um calibration (attrs + an um coordinate)
    assert ds["cam.image_x"].attrs["um_per_px"] == 0.5
    assert np.allclose(ds["cam.image_x_um"].values, np.arange(W) * 0.5)
    # the newest frame rides along for the live view (in memory only)
    idx, frame = FS.latest_frames(ds)["cam.image"]
    assert idx == (4,) and frame[1, 1] == 411

    path = write_dataset(ds, tmp_path / "img.nc")
    with h5py.File(path, "r") as f:
        v = f["cam.image"]
        assert v.dtype == np.uint16 and v.chunks == (1, H, W)
        assert v.compression == "gzip"
    back = xr.open_dataset(path, engine="h5netcdf")
    try:
        assert np.array_equal(back["cam.image"].values[2], _frame_for(2.0))
        assert back["cam.image_x_um"].attrs["units"] == "um"
    finally:
        back.close()


def test_an_aborted_map_marks_the_points_never_measured(tmp_path):
    reg, state = _camera_registry()
    seen = {"n": 0}

    def stop_after_three():
        return seen["n"] >= 3

    def on_point(done, total, snap):
        seen["n"] = done
    ds = run(_recipe(), reg, created_iso="t", should_abort=stop_after_three,
             on_point=on_point)
    assert list(ds["cam.image_measured"].values) == [True, True, True, False, False]
    path = write_dataset(ds, tmp_path / "part.nc")
    back = xr.open_dataset(path, engine="h5netcdf")
    try:
        img = back["cam.image"].values
        assert np.isnan(img[3]).all() and np.isnan(img[4]).all()   # fill -> NaN
        assert np.array_equal(img[1], _frame_for(1.0))
        assert list(back["cam.image_measured"].values) == [1, 1, 1, 0, 0]
    finally:
        back.close()


def test_a_frame_beyond_its_declared_bits_stops_the_scan():
    reg, state = _camera_registry(bits=12)
    g = reg.get("cam.image")
    g._get = lambda: np.full((H, W), 5000, dtype=np.uint16)     # 13 bit
    with pytest.raises(StorageError, match="outside 12 bits"):
        run(_recipe(n=2), reg, created_iso="t")


def test_bits_with_a_max_stays_unsigned_and_narrow():
    """A camera declares bits (= unsigned) AND max: a 4 x 4 bin of 12-bit
    counts is 16 bits but never above 65520 -- uint16, 65535 spare."""
    st = Storage.from_descriptor({"type": "int", "bits": 16, "max": 65520})
    assert (str(st.disk), st.fill) == ("uint16", 65535)
    st = Storage.from_descriptor({"type": "int", "bits": 14, "min": 0, "max": 16380})
    assert (str(st.disk), st.fill) == ("uint16", 65535)
    assert str(Storage.from_descriptor({"type": "int", "bits": 16}).disk) == "uint32"
    with pytest.raises(StorageError):
        Storage("int", bits=16, hi=65520).to_memory(np.array([65530]), what="d")


def test_the_vectorised_check_says_what_the_old_one_said():
    st = Storage("int", bits=12)
    frame = np.zeros((3, 4))
    frame[1, 2] = 2.5
    with pytest.raises(StorageError) as vec:
        st.to_memory(frame, what="d")
    with pytest.raises(StorageError) as loop:
        st.to_memory(frame.tolist(), what="d")
    assert str(vec.value) == str(loop.value)
    ok = st.to_memory(np.array([[np.nan, 4095]]), what="d")
    assert np.isnan(ok[0, 0]) and ok[0, 1] == 4095.0


def test_averaged_repeats_of_frames_average_pixel_by_pixel(tmp_path):
    reg, state = _camera_registry()
    r = Recipe(name="avg", axes=[{"type": "repeat", "num": 3, "mode": "average"},
                                 {"type": "linear", "param": "x", "start": 0,
                                  "stop": 1, "num": 2}],
               detectors=["cam.image"])
    ds = run(r, reg, created_iso="t")
    assert ds["cam.image"].dims == ("x", "cam.image_y", "cam.image_x")
    assert ds["cam.image_measured"].dims == ("x",)
    assert ds["cam.image_measured"].values.all()
    write_dataset(ds, tmp_path / "avg.nc")


# ───────────────────── write as you go (big maps) ────────────────────────────

@pytest.fixture
def small_limit(monkeypatch):
    """A "big" map at test size: anything above 100 bytes goes to the file
    (5 frames of 6 x 8 x 2 bytes = 480)."""
    monkeypatch.setattr(FS, "INCREMENTAL_ABOVE_BYTES", 100)


def test_a_big_map_is_written_into_the_file_frame_by_frame(tmp_path, small_limit):
    reg, state = _camera_registry()
    path = tmp_path / "big.nc"
    sizes = []

    def on_point(done, total, snap):
        ds = snap()
        assert "cam.image" not in ds.data_vars       # never in memory ...
        with h5py.File(path, "r") as f:              # ... but already on disk
            sizes.append(int(np.count_nonzero(
                f["cam.image"][...] != 65535) > 0))
        if done == 3:
            write_dataset(ds, path)                  # a checkpoint, in place
    ds = run(_recipe(), reg, created_iso="t", data_path=path, on_point=on_point)
    assert sizes and all(sizes)
    assert ds.attrs["images_written_as_they_came"] == "cam.image"
    assert FS.frames_of(ds)["cam.image"].n_written == 5
    write_dataset(ds, path)                          # the end, in place
    back = xr.open_dataset(path, engine="h5netcdf")
    try:
        img = back["cam.image"]
        assert img.dims == ("x", "cam.image_y", "cam.image_x")
        assert np.array_equal(img.values[4], _frame_for(4.0))
        assert img.attrs["declared_bits"] == 12 and img.attrs[FS.IMAGE_ATTR] == 1
        assert back["cam.image_measured"].values.all()
        assert back.attrs["n_points"] == 5
    finally:
        back.close()
    with h5py.File(path, "r") as f:
        assert f["cam.image"].chunks == (1, H, W) and f["cam.image"].compression == "gzip"

    # "Save data as" elsewhere copies the frames along
    other = write_dataset(ds, tmp_path / "copy" / "saved.nc")
    back = xr.open_dataset(other, engine="h5netcdf")
    try:
        assert np.array_equal(back["cam.image"].values[0], _frame_for(0.0))
    finally:
        back.close()


def test_an_aborted_big_map_keeps_its_frames_and_marks_the_rest(tmp_path, small_limit):
    reg, state = _camera_registry()
    path = tmp_path / "abort.nc"
    seen = {"n": 0}
    ds = run(_recipe(), reg, created_iso="t", data_path=path,
             should_abort=lambda: seen["n"] >= 2,
             on_point=lambda d, t, s: seen.__setitem__("n", d))
    write_dataset(ds, path)
    back = xr.open_dataset(path, engine="h5netcdf")
    try:
        img = back["cam.image"].values
        assert np.array_equal(img[1], _frame_for(1.0))
        assert np.isnan(img[2:]).all()
        assert list(back["cam.image_measured"].values) == [1, 1, 0, 0, 0]
    finally:
        back.close()


def test_a_big_map_without_a_data_file_is_refused_with_its_size(small_limit):
    reg, state = _camera_registry()
    with pytest.raises(ValueError, match=r"needs ~\d.*no data file"):
        run(_recipe(), reg, created_iso="t")
    assert state["grabs"] == 0                       # refused before anything


def test_a_big_averaged_map_is_refused(tmp_path, small_limit):
    reg, state = _camera_registry()
    r = Recipe(name="avg", axes=[{"type": "repeat", "num": 3, "mode": "average"},
                                 {"type": "linear", "param": "x", "start": 0,
                                  "stop": 1, "num": 2}],
               detectors=["cam.image"])
    with pytest.raises(ValueError, match="AVERAGED repeat"):
        run(r, reg, created_iso="t", data_path=tmp_path / "a.nc")


# ───────────────────────────── the estimate ──────────────────────────────────

def test_the_size_estimate():
    reg, _ = _camera_registry()
    est = FS.estimate(_recipe(n=5), reg)
    assert est == {"cam.image": 5 * H * W * 2}           # uint16
    # the camera's full frame, a 30 x 30 map: the roadmap's example
    g = reg.get("cam.image")
    g.axes[0].length, g.axes[1].length = 1096, 1936
    r = Recipe(name="m", axes=[{"type": "linear", "param": "x", "start": 0,
                                "stop": 1, "num": 900}], detectors=["cam.image"])
    nbytes = FS.estimate(r, reg)["cam.image"]
    assert nbytes == 900 * 1096 * 1936 * 2
    assert FS.format_bytes(nbytes) == "~3.8 GB"
    assert FS.format_bytes(2.1e9) == "~2.1 GB"
    assert FS.format_bytes(350e6) == "~350 MB"
    # averaged repeats store one frame per point
    ra = Recipe(name="a", axes=[{"type": "repeat", "num": 4, "mode": "average"},
                                {"type": "linear", "param": "x", "start": 0,
                                 "stop": 1, "num": 10}], detectors=["cam.image"])
    assert FS.estimate(ra, reg)["cam.image"] == 10 * 1096 * 1936 * 2
    # nothing for a scan without an image
    assert FS.estimate(Recipe(name="n", axes=[], detectors=[]), reg) == {}


# ─────────────────────────────── AaltoView ───────────────────────────────────

def test_aaltoview_slices_a_frame_and_averages_frames(tmp_path):
    """The viewer needs no change: an image's two pixel axes are 'just more
    rows' of the cube. One point's frame, and the mean frame over the scan."""
    from scan_core.data import load
    reg, _ = _camera_registry()
    path = write_dataset(run(_recipe(), reg, created_iso="t"), tmp_path / "v.nc")
    ds = load(path)
    try:
        da = ds["cam.image"]
        one = reduce_cube(da, x="cam.image_x", y="cam.image_y",
                          slices={"x": Slice("at", 2)})
        assert one.data.dims == ("cam.image_y", "cam.image_x")
        assert np.array_equal(one.data.values, _frame_for(2.0))
        mean = reduce_cube(da, x="cam.image_x", y="cam.image_y",
                           slices={"x": Slice("mean")})
        assert mean.data.values[1, 1] == pytest.approx(np.mean([100 * k + 11 for k in range(5)]))
        # and the other way round: one pixel against the scan axis
        line = reduce_cube(da, x="x", y="cam.image_y",
                           slices={"cam.image_x": Slice("at", 3)})
        assert line.data.dims == ("cam.image_y", "x")
    finally:
        ds.close()
