"""lab.py -- the REAL instrument registry, the counterpart of build_sim_registry.

`build_sim_registry()` in registry.py gives you toy physics with no hardware.
This module gives you the same Registry shape backed by the actual ZeroMQ
services, so a recipe written against the simulator runs unchanged against the
lab. That is the point of putting everything behind Parameters.

Adding a knob here is a DECLARATION, not code: name the verb that sets it, the
status field that reads it back, and the settle policy that says when it has
arrived. `instrument.py` does the rest, and the knob then appears in the Scan
Builder, is scannable on any axis, and lands in the dataset -- with no other
edits anywhere. That is the expandability guarantee the old LabVIEW VI could not
make.

Two things are deliberately NOT hardcoded here:

* **Limits come from the instrument.** Each service reports its own safety
  envelope in its `info` block, and we read the Settable's limits from that. A
  limit written down in two places is a limit that will disagree with itself.
* **Which instruments to connect is the caller's choice.** You rarely want all
  seven; `include=` selects, and only those services need to be running.

Usage:

    from scan_core.lab import build_lab_registry
    reg, lab = build_lab_registry(include=("clMag",))
    try:
        ds = run(recipe, reg)
    finally:
        lab.close()
"""

from __future__ import annotations

from suite_common import discover

from .instrument import (Instrument, InstrumentError, adopt_then_flag, echoes,
                         flag_only, immediate)
from .manifest import describe_or_none, register_manifest
from .registry import Gettable, Registry, Settable


# Which modules exist and where they listen is NOT typed here any more. It
# comes from module discovery (suite-common): every <folder>/module.toml, plus
# this PC's suite_local.json, which the launcher writes. So a port changed in
# the launcher is the port scan-core dials, and a new module needs no edit here.

def known_ports(root=None) -> dict[str, tuple[int, int]]:
    """{key: (cmd, pub)} for every LOCAL module, with this PC's overrides."""
    return {m.key: (m.cmd, m.pub) for m in discover(root).modules if not m.remote}


def module_prefix(inst: Instrument, manifest: dict | None = None) -> str:
    """The name a connected instrument's parameters are prefixed with.

    Normally the module name its own `describe` reports ("hf2" -> "hf2.r1").
    A service added by hand from another PC carries an `alias` (its slug,
    "hf2_lab2"), so two lock-ins do not both become "hf2.r1".
    """
    alias = getattr(inst, "alias", None)
    if alias:
        return alias
    manifest = manifest if manifest is not None else (getattr(inst, "manifest", None) or {})
    return manifest.get("module", inst.name)


class Lab:
    """The set of instrument connections a registry is built on.

    Exists so the caller has one thing to close. A scan that leaves sockets and
    SUB threads behind will happily run again in the same process and quietly
    accumulate them.
    """

    def __init__(self):
        self.instruments: dict[str, Instrument] = {}

    def connect(self, name: str, host: str = "localhost",
                cmd_port: int | None = None, timeout_ms: int = 3000,
                pub_port: int | None = None, alias: str | None = None) -> Instrument:
        if cmd_port is None:
            ports = known_ports()
            if name not in ports:
                raise ValueError(f"unknown instrument {name!r}; known: {sorted(ports)}")
            cmd_port, pub_port = ports[name]
        inst = Instrument(name, host=host, cmd_port=cmd_port, pub_port=pub_port,
                          timeout_ms=timeout_ms)
        inst.alias = alias
        try:
            inst.info = inst.start()      # fails fast if the service is not up
        except Exception:
            inst.close()
            raise
        self.instruments[name] = inst
        return inst

    def __getitem__(self, name: str) -> Instrument:
        return self.instruments[name]

    def set_abort(self, should_abort) -> None:
        """Let every instrument see the operator's Abort DURING a settle wait.

        The engine only checks `should_abort` between points, so a knob that
        never settles (a stabiliser that cannot reach its point, a magnet with
        no calibration) makes Abort look dead for the length of the timeout.
        Pass None to clear it.
        """
        for inst in self.instruments.values():
            inst.should_abort = should_abort

    def close(self) -> None:
        for inst in self.instruments.values():
            inst.close()
        self.instruments.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- keeping the inherited limits honest -----------------------------

    def refresh_stale(self, registry, prefix: bool = False, on_warn=None) -> list:
        """Re-read `describe` from any service whose manifest went stale, and
        push the new LIMITS onto the matching registry parameters.

        This is the other half of `describe_rev`, and without it the inheritance
        is a one-time snapshot. A Settable's limits are baked when the manifest
        is first read, but the limits themselves move:

          * piezo's travel ceiling drops 200 -> 160 um when an axis goes
            closed-loop
          * kim's armed leash REPLACES the travel clamp outright
          * clMag's field range IS the loaded calibration, so measuring or
            loading one changes it

        A registry built before any of that lets a recipe sweep past what the
        instrument will now accept. Nothing raises: the service clamps, and the
        scan records a grid of points it never visited at the coordinates it
        thinks it did.

        Comparing is cheap -- one integer per instrument, already in the status
        stream -- so call this before a scan starts and whenever a panel is
        about to show a range.

        Returns the ids whose limits actually changed, so a caller can say so.
        """
        changed = []
        warn = on_warn or (lambda m: None)

        for name, inst in self.instruments.items():
            cached = getattr(inst, "manifest", None)
            if not cached:
                continue                      # built from the fallback declaration
            live_rev = (inst.status() or {}).get("describe_rev")
            if live_rev is None or live_rev == cached.get("revision"):
                continue                      # unchanged, or a service that
                                              # does not publish the revision
            try:
                fresh = inst.command("describe").get("describe")
            except Exception as exc:
                warn(f"{name}: could not re-read describe ({exc}); "
                     f"limits may be stale")
                continue
            if not fresh:
                continue
            inst.manifest = fresh
            changed += _apply_limits(registry, fresh, prefix, warn,
                                     module=module_prefix(inst, fresh))

        return changed


def _apply_limits(registry, manifest: dict, prefix: bool, warn, module=None) -> list:
    """Copy a fresh manifest's bounds onto the registry's existing parameters.

    Deliberately updates limits (and label/unit) IN PLACE rather than rebuilding
    the registry: a running Scan Builder holds references to these Parameter
    objects in its axis rows, and swapping them out from under it would leave
    the UI pointing at orphans.
    """
    module = module or manifest.get("module", "")
    changed = []
    for d in manifest.get("parameters", []):
        if d.get("kind") != "control":
            continue
        pid = f"{module}.{d['id']}" if prefix else d["id"]
        param = registry.get(pid)
        if param is None or not hasattr(param, "limits"):
            continue
        lo, hi = d.get("min"), d.get("max")
        if d.get("type") == "bool":
            lo, hi = 0, 1
        else:
            lo = float("-inf") if lo is None else float(lo)
            hi = float("inf") if hi is None else float(hi)
        if (lo, hi) != tuple(param.limits):
            old = tuple(param.limits)
            param.limits = (lo, hi)
            changed.append(pid)
            warn(f"{pid}: limits changed {old[0]:g}..{old[1]:g} -> {lo:g}..{hi:g}")
    return changed


# ------------------------- declaring one knob -------------------------------

def remote_settable(reg: Registry, inst: Instrument, *, id: str, label: str,
                    unit: str, limits, verb: str, arg: str, read_key: str,
                    settle, timeout_s: float = 60.0, scale: float = 1.0,
                    **extra) -> Settable:
    """Register one settable knob backed by a service verb.

    `scale` converts the unit you scan in to the unit the wire speaks (RF
    frequency is scanned in MHz and commanded in Hz, so scale=1e6). Keeping the
    conversion here means the recipe, the axis label and the dataset coordinate
    all stay in the unit a physicist actually wants to type.

    The returned Settable's `set` BLOCKS until the settle policy is satisfied,
    which is exactly the contract engine.py relies on: when `set` returns, the
    detectors may be read.
    """
    def set_fn(value: float):
        inst.command(verb, **{arg: value * scale}, **extra)
        inst.wait_until(settle(value * scale), timeout_s=timeout_s,
                        what=f"{id} = {value:g} {unit}")

    def get_fn():
        v = inst.status().get(read_key)
        return None if v is None else float(v) / scale

    return reg.add(Settable(id, label, unit, limits, set_fn=set_fn, get_fn=get_fn))


def remote_gettable(reg: Registry, inst: Instrument, *, id: str, label: str,
                    unit: str, read_key: str | None = None,
                    verb: str | None = None, reply_key: str | None = None,
                    scale: float = 1.0, **extra) -> Gettable:
    """Register one detector backed by a service.

    Two flavours, and the difference matters for data quality:

    * `read_key` reads the cached status stream -- cheap, but up to one status
      period (~100 ms) old. Right for context channels like the magnet current.
    * `verb`/`reply_key` issues a command for a FRESH sample. Right for anything
      you are actually measuring, where a stale value is a wrong data point.
    """
    if verb is not None:
        def get_fn():
            r = inst.command(verb, **extra)
            return float(r.get(reply_key, "nan")) / scale
    else:
        def get_fn():
            v = inst.status().get(read_key)
            return float("nan") if v is None else float(v) / scale

    return reg.add(Gettable(id, label, unit, get_fn))


# ------------------------- the lab registry ---------------------------------

def build_lab_registry(host: str = "localhost", include=("clMag",),
                       aux_ai_channels=("Dev1/ai1", "Dev1/ai2", "Dev1/ai3"),
                       timeout_ms: int = 3000, ports: dict | None = None,
                       prefix: bool = False, force_builtin: bool = False,
                       on_warn=None, endpoints: dict | None = None):
    """Connect to the named services and return (Registry, Lab).

    Only the services in `include` need to be running. Start them from
    mission-control, or individually with `uv run scripts/run_service.py`.

    Where each one listens, in order of precedence:
      * `endpoints` {name: (host, cmd, pub)} -- what the measurement suite
        passes, straight from module discovery. The name is used as the
        parameter prefix, so a remote "hf2_lab2" cannot collide with a local hf2.
      * `ports` {name: cmd} on `host` -- for tests against a scratch port.
      * otherwise module discovery: the module's ports on THIS PC (its
        module.toml, or the override set in the launcher), on `host`.

    `prefix=True` namespaces parameter ids as "<module>.<id>", which you want as
    soon as two modules are connected, since several own a knob called
    `position`. `force_builtin=True` ignores `describe` and uses scan-core's
    hand-written declarations, which is only useful for comparing the two.
    `on_warn` receives non-fatal problems (a module with no `describe`, an
    unrecognised settle policy) instead of them vanishing.
    """
    reg = Registry()
    lab = Lab()
    ports = ports or {}
    endpoints = endpoints or {}
    warn = on_warn or (lambda m: None)
    try:
        for name in include:
            if name in endpoints:
                ep_host, cmd, pub = endpoints[name]
                inst = lab.connect(name, host=ep_host, cmd_port=int(cmd),
                                   pub_port=int(pub), timeout_ms=timeout_ms,
                                   alias=name)
            else:
                # lab.connect looks the ports up in discovery when not given,
                # and refuses a name nothing is known about
                inst = lab.connect(name, host=host, cmd_port=ports.get(name),
                                   timeout_ms=timeout_ms)

            # PREFER the module's own self-description. Everything a Parameter
            # needs -- limits, verb, status key, settle rule -- already lives in
            # the module that owns the hardware, so asking is always better than
            # keeping a second copy here that can disagree with it.
            manifest = None if force_builtin else describe_or_none(inst)
            if manifest is not None:
                inst.manifest = manifest
                register_manifest(reg, inst, manifest, prefix=prefix,
                                  on_warn=warn, module_name=inst.alias)
                continue

            # Fall back to the hand-written declaration for modules that have
            # not been taught `describe` yet. Without this the coordinator would
            # be useless mid-rollout.
            builder = _BUILDERS.get(name)
            if builder is None:
                raise InstrumentError(
                    f"{name}: does not answer `describe` and scan-core has no "
                    f"built-in declaration for it. Teach the module to describe "
                    f"itself (see clMag's net/describe.py).")
            warn(f"{name}: no `describe` verb; using scan-core's built-in "
                 f"declaration, which may not match the running service")
            builder(reg, inst, aux_ai_channels=aux_ai_channels)
    except Exception:
        lab.close()          # never leave half-open sockets behind
        raise
    return reg, lab


def _build_clMag(reg: Registry, inst: Instrument, aux_ai_channels=(), **_):
    """Magnet field controller -- the closed-loop case.

    `field` is the interesting one. It is a genuine closed-loop knob: the
    service ramps, runs a PI seek, and only then raises `field_stable`. The
    settle policy has to wait for the setpoint to be ADOPTED before believing
    that flag, or the first poll after a set returns the previous point's
    success. `adopt_then_flag` is exactly that, and clMag is why it exists.
    """
    info = inst.info
    lo, hi = info.get("field_lo", 0.0), info.get("field_hi", 0.0)
    if lo == 0.0 and hi == 0.0:
        raise InstrumentError(
            "clMag reports no calibration (field range 0..0), so it will refuse "
            "every setpoint. Run a calibration in the magnet GUI, or load a "
            "saved one, before scanning field.")

    remote_settable(
        reg, inst,
        id="field", label="Magnetic field", unit="mT",
        limits=(lo, hi),                       # the magnet's own envelope
        verb="set_field", arg="field_mT", read_key="measured_field_mT",
        settle=adopt_then_flag("setpoint_field_mT", "field_stable"),
        timeout_s=60.0, use_pid=True,
    )

    # Context channels: cheap, read from the status stream.
    # These ids MUST match what clMag's own `describe` reports (measured_field,
    # current). This fallback once used field_measured / magnet_current, so the
    # same instrument had two sets of ids depending on which path built the
    # registry -- a saved recipe then worked against one and failed against the
    # other. The module is the source of truth; this copy follows it.
    remote_gettable(reg, inst, id="measured_field", label="Measured field",
                    unit="mT", read_key="measured_field_mT")
    remote_gettable(reg, inst, id="current", label="Magnet current",
                    unit="A", read_key="current_A")

    # The AUX analog inputs on the same NI card are the actual detectors -- the
    # photodiode / Kerr signal lands on one of these. Fresh single samples, not
    # the status cache, because these are the measurement.
    for ch in aux_ai_channels:
        short = ch.split("/")[-1]              # "Dev1/ai1" -> "ai1"
        remote_gettable(reg, inst, id=f"aux_{short}", label=f"AUX {short}",
                        unit="V", verb="aux_read_ai", reply_key="volts",
                        channel=ch)


def _build_smb(reg: Registry, inst: Instrument, **_):
    """RF generator -- the set-and-forget case.

    Nothing converges here, so there is no stable flag to wait on. What the
    service does report is the value it currently holds, so `echoes` waits for
    that confirmation: weaker than a closed-loop settle, but a real check rather
    than a sleep.
    """
    info = inst.info
    remote_settable(
        reg, inst,
        id="rf_freq", label="RF frequency", unit="MHz",
        limits=(info.get("freq_min_Hz", 9e3) / 1e6,
                info.get("freq_max_Hz", 6e9) / 1e6),
        verb="set_frequency", arg="frequency_Hz", read_key="frequency_Hz",
        settle=echoes("frequency_Hz", tol=1.0),   # 1 Hz out of GHz
        scale=1e6, timeout_s=10.0,
    )
    remote_settable(
        reg, inst,
        id="rf_power", label="RF power", unit="dBm",
        limits=(info.get("power_min_dBm", -145.0), info.get("power_max_dBm", 18.0)),
        verb="set_power", arg="power_dBm", read_key="power_dBm",
        settle=echoes("power_dBm", tol=1e-3), timeout_s=10.0,
    )
    remote_settable(
        reg, inst,
        id="rf_phase", label="RF phase", unit="deg",
        limits=(info.get("phase_min_deg", -360.0), info.get("phase_max_deg", 360.0)),
        verb="set_phase", arg="phase_deg", read_key="phase_deg",
        settle=echoes("phase_deg", tol=1e-3), timeout_s=10.0,
    )


#: name -> the function that declares that instrument's parameters.
#: Only clMag and smb are declared so far. The other five services speak the same
#: contract, so adding one is a `_build_*` function here and nothing else -- but
#: each needs its settle signal confirmed against the running service first
#: (stage/piezo/kim report a `moving` flag, which is `flag_only(..., invert=True)`
#: or `adopt_then_flag(..., invert=True)` if they echo a target). None of them
#: has been checked yet, so none is guessed at here.
_BUILDERS = {
    "clMag": _build_clMag,
    "smb": _build_smb,
}
