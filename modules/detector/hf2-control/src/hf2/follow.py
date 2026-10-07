"""follow.py -- let a setting FOLLOW another module's value through a formula.

The use that asked for it (Lukas, 2026-10-07): super-Nyquist MOKE. The laser
pulses at 80 MHz, so an 810 MHz magnetisation precession is sampled down to
810 - 10 * 80 = 10 MHz, and that is where the lock-in must demodulate. Every
time the RF generator's frequency changes, the demodulation frequency has to
change with it. Typed by hand that is one more thing to forget per point; here
it is one line of configuration:

    source   smb.frequency_Hz          (module key . status key)
    formula  alias(x, 80e6)            (x = the source's value)

and the lock-in follows by itself -- whether the RF frequency was changed by a
scan, a script or a hand on the generator's GUI.

It is GENERIC: nothing in here knows about lock-ins or generators. A module
gives a Follower the source, the formula and a function that applies a number
(hf2: set the demodulator's oscillator). Any other module can do the same.

How it hears the source -- the same two channels every client uses:
  * its PUB status stream (raw pyzmq, ~10 Hz): live following for the panel.
    A value is applied only when the SOURCE VALUE CHANGES, so a steady
    generator does not hammer the lock-in with ten identical sets a second.
  * one REQ `status` on demand: `sync()`. A scan needs it. scan-core sets the
    generator, waits until the GENERATOR reports the new frequency, then
    triggers the lock-in. The lock-in's own subscription may still be a few
    milliseconds behind at that moment -- so before it starts its settle
    clock, the lock-in ASKS the generator directly (sync) and applies the
    answer. No race, no "it usually arrives in time".

Rules it keeps (suite-wide):
  * no import of the source's package (raw pyzmq, decoupled projects);
  * encrypted where the lab's policy secures the source (secure.py);
  * the socket lives in ONE thread (ZeroMQ sockets are not thread-safe);
  * a bad frame or a formula that cannot be evaluated never kills the thread:
    it is reported (rate-limited) and the last good value stays.

The formula is NOT Python's eval(). It is parsed and only arithmetic, numbers,
`x` and a short list of functions are allowed (FUNCTIONS below), so a config
file or a remote client cannot run code in the service.

suite-common holds the MASTER copy; a module that uses it carries a
byte-identical copy in src/<pkg>/follow.py (tools/check_modules.py compares).
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import threading
import time

import zmq

from . import secure


# ---------------------------------------------------------------- formula ---

def alias(f: float, fs: float) -> float:
    """Where a frequency f lands when sampled at rate fs (folded into 0..fs/2).

    The distance from f to the nearest multiple of fs:
        alias(810e6, 80e6) = |810 - 10 * 80| MHz = 10 MHz
        alias(790e6, 80e6) = |790 - 10 * 80| MHz = 10 MHz   (the mirror image)
    The sign is lost, which is what a lock-in sees: it demodulates at a
    positive frequency, and a mirrored line only flips the sign of its phase.
    """
    fs = float(fs)
    if not fs > 0:
        raise ValueError(f"alias: sampling rate must be > 0, got {fs!r}")
    return abs(f - round(f / fs) * fs)


def fold(f: float, fs: float) -> float:
    """f modulo fs (0..fs), keeping which side of a harmonic the line is on:
    fold(810e6, 80e6) = 10 MHz, fold(790e6, 80e6) = 70 MHz."""
    fs = float(fs)
    if not fs > 0:
        raise ValueError(f"fold: sampling rate must be > 0, got {fs!r}")
    return f % fs


#: The functions a formula may call. Short on purpose; add one when it is needed.
FUNCTIONS = {
    "alias": alias, "fold": fold,
    "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt,
}
#: Named constants a formula may use besides `x`.
CONSTANTS = {"pi": math.pi}

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


class Formula:
    """A checked arithmetic expression in one variable, `x`.

    Formula("alias(x, 80e6)")(810e6) -> 10e6.  Formula("") is the identity.
    Construction raises ValueError for anything that is not allowed, so a bad
    formula is refused when it is SET, not at the first scan point.
    """

    def __init__(self, text: str):
        self.text = str(text or "").strip()
        src = self.text or "x"
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"formula {self.text!r} is not valid: {exc.msg}") from None
        self._check(tree.body)
        self._tree = tree.body

    def __call__(self, x: float) -> float:
        v = float(self._eval(self._tree, float(x)))
        if not math.isfinite(v):
            raise ValueError(f"formula {self.text!r} gave {v} for x = {x:g}")
        return v

    def _check(self, node) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"formula {self.text!r}: only numbers are allowed, "
                                 f"not {node.value!r}")
        elif isinstance(node, ast.Name):
            if node.id != "x" and node.id not in CONSTANTS:
                raise ValueError(f"formula {self.text!r}: unknown name {node.id!r} "
                                 f"(the source value is x)")
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BINARY:
                raise ValueError(f"formula {self.text!r}: operator not allowed")
            self._check(node.left)
            self._check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY:
                raise ValueError(f"formula {self.text!r}: operator not allowed")
            self._check(node.operand)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ValueError(f"formula {self.text!r}: allowed functions are "
                                 + ", ".join(sorted(FUNCTIONS)))
            if node.keywords:
                raise ValueError(f"formula {self.text!r}: no keyword arguments")
            for a in node.args:
                self._check(a)
        else:
            raise ValueError(f"formula {self.text!r}: only arithmetic, x, numbers "
                             f"and {', '.join(sorted(FUNCTIONS))} are allowed")

    def _eval(self, node, x):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return x if node.id == "x" else CONSTANTS[node.id]
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, x), self._eval(node.right, x)
            if isinstance(node.op, ast.Pow) and abs(b) > 100:
                # 10 ** 10 ** 10 would hang the service computing a huge integer
                raise ValueError(f"formula {self.text!r}: exponent {b:g} too large")
            try:
                return _BINARY[type(node.op)](a, b)
            except (ZeroDivisionError, OverflowError) as exc:
                raise ValueError(f"formula {self.text!r} at x = {x:g}: {exc}") from None
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, x))
        return FUNCTIONS[node.func.id](*(self._eval(a, x) for a in node.args))


# ----------------------------------------------------------------- source ---

def parse_source(text: str) -> tuple[str, list]:
    """'smb.frequency_Hz' -> ('smb', ['frequency_Hz']).

    The part before the first dot is the MODULE KEY (as in module.toml and the
    launcher), the rest a path into its status frame. A number in the path is a
    list index: 'hf2.ref_freq_Hz.0' = channel 1 of a per-channel list.
    """
    t = str(text or "").strip()
    module, _, rest = t.partition(".")
    if not module or not rest:
        raise ValueError(f"source {text!r} must be <module>.<status key>, "
                         f"e.g. smb.frequency_Hz")
    path = [int(p) if p.lstrip("-").isdigit() else p for p in rest.split(".")]
    return module, path


def read_path(frame, path):
    """The number at `path` in a status frame, or None if it is not there."""
    cur = frame
    for key in path:
        if isinstance(key, int) and isinstance(cur, list):
            if not -len(cur) <= key < len(cur):
                return None
            cur = cur[key]
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur) if math.isfinite(cur) else None


def resolve_endpoint(module: str, endpoint: str = "") -> tuple[str, int, int]:
    """Where `module` listens: (host, cmd_port, pub_port).

    `endpoint` = "host:cmd:pub" typed in the config wins. Otherwise the table
    the launcher gives every process it starts (AALTOFLOW_ENDPOINTS). A
    service started by hand, with neither, cannot know -- and says so.
    """
    e = str(endpoint or "").strip()
    if e:
        parts = e.rsplit(":", 2)
        if len(parts) != 3:
            raise ValueError(f"endpoint {endpoint!r} must be host:cmd_port:pub_port")
        host, cmd, pub = parts
        return host, int(cmd), int(pub)
    raw = os.environ.get("AALTOFLOW_ENDPOINTS") or os.environ.get("TRMOKE_ENDPOINTS")
    try:
        table = json.loads(raw) if raw else {}
    except ValueError:
        table = {}
    ep = table.get(module) if isinstance(table, dict) else None
    if not ep or len(ep) != 3:
        raise ValueError(f"where does {module!r} listen? Start this service from "
                         f"Mission Control, or give the endpoint as host:cmd:pub")
    host, cmd, pub = ep
    return ("127.0.0.1" if host == "localhost" else str(host)), int(cmd), int(pub)


# --------------------------------------------------------------- follower ---

class Follower:
    """Apply formula(source value) whenever the source value changes.

    apply_fn(value) does the actual work (hf2: set the oscillator). It is
    called from the listener thread, or from the caller's thread in sync();
    calls never overlap (one lock), so the last value applied is the last one
    heard.
    """

    def __init__(self, source: str, formula: str, apply_fn, *,
                 endpoint: str = "", on_event=None, clock=time.monotonic,
                 timeout_s: float = 1.0):
        self.source = str(source).strip()
        self.module, self.path = parse_source(self.source)
        self.formula = Formula(formula)
        self.host, self.cmd_port, self.pub_port = resolve_endpoint(self.module, endpoint)
        self._apply = apply_fn
        self._say = on_event or (lambda level, msg: None)
        self._clock = clock
        self.timeout_s = float(timeout_s)

        self._apply_lock = threading.Lock()
        self._x = None               # last source value APPLIED
        self._target = None          # formula(x) that was applied
        self._t = None               # when the source was last heard
        self._error = ""
        self._last_say = -1e9

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen, daemon=True,
                                        name=f"follow-{self.module}")
        self._thread.start()

    # -- public ---------------------------------------------------------------

    def sync(self) -> float:
        """Ask the source for its value NOW, apply it, return the target.

        Raises ValueError, saying why, if the source does not answer or has
        no such value: a caller that needs the right value (a scan point) must
        not go on with a stale one.
        """
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, int(self.timeout_s * 1000))
        try:
            secure.secure_client(s, self.host, self.module)
            s.connect(f"tcp://{self.host}:{self.cmd_port}")
            s.send(json.dumps({"cmd": "status"}).encode("utf-8"))
            try:
                reply = json.loads(s.recv())
            except zmq.Again:
                raise ValueError(f"{self.module} at {self.host}:{self.cmd_port} did not "
                                 f"answer within {self.timeout_s:g} s") from None
        finally:
            s.close(0)
        frame = reply.get("status") if isinstance(reply, dict) else None
        x = read_path(frame, self.path) if isinstance(frame, dict) else None
        if x is None:
            raise ValueError(f"{self.source}: no such number in {self.module}'s status")
        return self._take(x)

    def state(self) -> dict:
        """For status frames: what is followed and where it stands."""
        age = None if self._t is None else self._clock() - self._t
        return {"source": self.source, "formula": self.formula.text,
                "x": self._x, "target": self._target,
                "age_s": age, "error": self._error}

    def close(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    # -- internals --------------------------------------------------------------

    def _take(self, x: float) -> float:
        with self._apply_lock:
            self._t = self._clock()
            if self._x is not None and x == self._x:
                return self._target
            try:
                target = self.formula(x)
                self._apply(target)
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                now = self._clock()
                if now - self._last_say >= 5.0:      # one event per 5 s, not 10 per s
                    self._last_say = now
                    self._say("error", f"follow {self.source}: {self._error}")
                raise ValueError(self._error) from None
            self._x, self._target, self._error = x, target, ""
            return target

    def _listen(self) -> None:
        def make_sub():
            s = zmq.Context.instance().socket(zmq.SUB)
            s.setsockopt(zmq.LINGER, 0)
            try:
                secure.secure_client(s, self.host, self.module)
            except secure.SecurityError as exc:
                self._error = str(exc)
                self._say("error", f"follow {self.source}: {exc}")
            s.connect(f"tcp://{self.host}:{self.pub_port}")   # lazy: fine if not up yet
            s.setsockopt(zmq.SUBSCRIBE, b"status")
            return s, secure.flip_generation()

        sub, gen = make_sub()
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request in this process found the source speaking the
                    # other mode than the policy says: rebuild (as vna does)
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = make_sub()
                    poller.register(sub, zmq.POLLIN)
                if not poller.poll(200):
                    continue
                try:
                    _topic, payload = sub.recv_multipart()
                    frame = json.loads(payload)
                except (ValueError, zmq.ZMQError):
                    continue                   # a malformed frame must not end the thread
                x = read_path(frame, self.path) if isinstance(frame, dict) else None
                if x is None:
                    continue
                try:
                    self._take(x)
                except ValueError:
                    pass                       # reported in _take; keep listening
        finally:
            sub.close(0)
