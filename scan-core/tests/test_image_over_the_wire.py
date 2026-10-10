"""A camera image detector in ANOTHER PROCESS: binary reply parts (2026-10-10).

The camera serves its frame by command (`read`), as a RAW binary part after
the JSON header when the request says "binary": true:

    part 0   {"ok": true, ..., "binary": [{"key": "image", "dtype": "<u2",
                                           "shape": [H, W]}]}
    part 1   H * W * 2 bytes, C order

and as {"dtype", "shape", "b64"} inside JSON otherwise. These tests drive a
fake camera that speaks only that contract (no camera package is imported:
scan-core knows the protocol, not the instruments):

* instrument.decode_reply rebuilds the arrays (and refuses a header that does
  not match the parts);
* a whole scan fetches a FRESH frame per point (numbered acquisitions) and
  stores them as uint16 with the pixel and um coordinates the module sends;
* a module that ignores "binary" (answers base64 JSON) decodes the same.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

from scan_core.autosave import write_dataset                       # noqa: E402
from scan_core.engine import run                                   # noqa: E402
from scan_core.instrument import Instrument, InstrumentError, decode_reply  # noqa: E402
from scan_core.manifest import decode_wire_value, register_manifest  # noqa: E402
from scan_core.recipe import Recipe                                # noqa: E402
from scan_core.registry import Registry, Settable                  # noqa: E402

H, W = 12, 16


def test_decode_reply_puts_the_binary_parts_where_the_header_says():
    a = (np.arange(H * W, dtype="<u2") * 7).reshape(H, W)
    head = {"ok": True, "image_meta": {"image_id": 3},
            "binary": [{"key": "image", "dtype": "<u2", "shape": [H, W]}]}
    r = decode_reply([json.dumps(head).encode(), a.tobytes()])
    assert "binary" not in r and r["image_meta"]["image_id"] == 3
    assert r["image"].dtype == np.dtype("<u2") and np.array_equal(r["image"], a)
    # an ordinary one-part reply is untouched
    assert decode_reply([b'{"ok": true, "x": 1}']) == {"ok": True, "x": 1}
    # a header that does not match what arrived is refused, never guessed
    with pytest.raises(InstrumentError, match="lists 1 binary part"):
        decode_reply([json.dumps(head).encode()])
    with pytest.raises(InstrumentError, match="bytes"):
        decode_reply([json.dumps(head).encode(), a.tobytes()[:-2]])
    # the JSON form (a client that did not ask for binary) decodes the same
    import base64
    j = {"dtype": "<u2", "shape": [H, W], "b64": base64.b64encode(a.tobytes()).decode()}
    assert np.array_equal(decode_wire_value(j), a)


class FakeCamera:
    """Acquisition n latches a 12-bit frame whose every pixel is n * 100 +
    its column (and one full-scale pixel), so each scan point can be checked
    for holding ITS frame. `binary` False = a module that ignores the flag."""

    def __init__(self, cmd_port, binary=True):
        self.cmd_port, self.pub_port = cmd_port, cmd_port + 1
        self.binary = binary
        self._lock = threading.Lock()
        self._id, self._acquiring, self._frame = 0, False, None
        self.asked_binary = []
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        acquire = {"group": "image", "trigger_verb": "acquire_image",
                   "target_key": "image_id",
                   "ready": {"policy": "adopt_then_flag", "setpoint_key": "image_id",
                             "flag_key": "image_acquiring", "invert": True}}
        self.manifest = {"schema": 1, "module": "camera", "revision": 1, "parameters": [
            {"id": "image", "label": "Camera image", "kind": "indicator", "type": "int",
             "dtype": "int", "unit": "counts", "min": 0, "max": 4095, "bits": 12,
             "acquire": acquire,
             "dims": [{"name": "image_y", "label": "image y", "unit": "px", "length": H,
                       "coord_verb": "image_coords", "coord_key": "y",
                       "attrs": {"um_per_px": 0.25},
                       "aux": [{"name": "image_y_um", "key": "y_um", "unit": "um"}]},
                      {"name": "image_x", "label": "image x", "unit": "px", "length": W,
                       "coord_verb": "image_coords", "coord_key": "x",
                       "attrs": {"um_per_px": 0.25},
                       "aux": [{"name": "image_x_um", "key": "x_um", "unit": "um"}]}],
             "read": {"verb": "get_image", "key": "image", "args": {"which": "sample"},
                      "binary": True}}]}

    def start(self):
        for fn in (self._serve, self._publish):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.2)

    def _expose(self, n):
        time.sleep(0.05)
        f = (n * 100 + np.arange(W)[None, :] + 0 * np.arange(H)[:, None]).astype("<u2")
        f[0, 0] = 4095
        with self._lock:
            self._frame = f
            self._acquiring = False

    def _handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "acquire_image":
            with self._lock:
                self._id += 1
                self._acquiring = True
                n = self._id
            threading.Thread(target=self._expose, args=(n,), daemon=True).start()
            return {"ok": True, "image_id": n}, []
        if cmd == "get_image":
            assert msg.get("which") == "sample"
            self.asked_binary.append(bool(msg.get("binary")))
            with self._lock:
                f = self._frame
            if self.binary and msg.get("binary"):
                return ({"ok": True, "image_meta": {"image_id": self._id},
                         "binary": [{"key": "image", "dtype": f.dtype.str,
                                     "shape": list(f.shape)}]}, [f.tobytes()])
            import base64
            return {"ok": True, "image": {"dtype": f.dtype.str, "shape": list(f.shape),
                                          "b64": base64.b64encode(f.tobytes()).decode()}}, []
        if cmd == "image_coords":
            y, x = np.arange(H, dtype=float), np.arange(W, dtype=float)
            return {"ok": True, "y": y.tolist(), "x": x.tolist(),
                    "y_um": (y * 0.25).tolist(), "x_um": (x * 0.25).tolist()}, []
        if cmd == "status":
            return {"ok": True, "status": self._status()}, []
        if cmd == "describe":
            return {"ok": True, "describe": self.manifest}, []
        return {"ok": False, "error": f"unknown {cmd}"}, []

    def _status(self):
        with self._lock:
            return {"image_id": self._id, "image_acquiring": self._acquiring}

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                try:
                    head, parts = self._handle(rep.recv_json())
                except Exception as exc:
                    head, parts = {"ok": False, "error": str(exc)}, []
                rep.send_multipart([json.dumps(head).encode()] + parts)
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.02)
        pub.close(0)


@pytest.mark.parametrize("port, binary", [(15980, True), (15982, False)])
def test_a_scan_records_a_fresh_frame_per_point(tmp_path, port, binary):
    svc = FakeCamera(port, binary=binary).start()
    inst = Instrument("camera", host="127.0.0.1", cmd_port=port)
    try:
        time.sleep(0.2)
        state = {"x": 0.0}
        reg = Registry()
        reg.add(Settable("x", "X", "um", (-10, 10),
                         set_fn=lambda v: state.__setitem__("x", v),
                         get_fn=lambda: state["x"]))
        register_manifest(reg, inst, inst.command("describe")["describe"], prefix=True)
        r = Recipe(name="t", axes=[{"type": "linear", "param": "x",
                                    "start": 0, "stop": 3, "num": 4}],
                   detectors=["camera.image"])
        ds = run(r, reg, created_iso="t")
        assert all(svc.asked_binary)                 # the descriptor's flag went out
        da = ds["camera.image"]
        assert da.dims == ("x", "camera.image_y", "camera.image_x")
        for i in range(4):
            n = i + 1                                # point i holds acquisition i+1
            assert da.values[i, 3, 5] == n * 100 + 5, f"point {i} has the wrong frame"
        assert ds["camera.image_x"].attrs["um_per_px"] == 0.25
        assert np.allclose(ds["camera.image_x_um"].values, np.arange(W) * 0.25)
        assert ds["camera.image_measured"].values.all()
        path = write_dataset(ds, tmp_path / "wire.nc")
        import h5py
        with h5py.File(path, "r") as f:
            assert f["camera.image"].dtype == np.uint16
            assert f["camera.image"].chunks == (1, H, W)
    finally:
        inst.close()
        svc.stop()
