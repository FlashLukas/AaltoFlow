"""lab.py -- the REAL instrument registry, the counterpart of build_sim_registry.

`build_sim_registry()` in registry.py gives you toy physics with no hardware.
This module gives you the same Registry shape backed by the actual ZeroMQ
services, so a recipe written against the simulator runs unchanged against the
lab. That is the point of putting everything behind Parameters.

scan-core knows the PROTOCOL, not the instruments: every knob comes from the
module's own `describe` manifest (INSTRUMENT_MODULE_GUIDE.md section 6b) --
the verb that sets it, the status field that reads it back, its live limits
and the settle policy that says when it has arrived. Nothing about any
particular instrument is written here, so a new module needs no edit in
scan-core: it appears in the Scan Builder, is scannable on any axis and lands
in the dataset. A service that does not answer `describe` is refused.

Two things are deliberately NOT hardcoded here:

* **Limits come from the instrument.** Each service reports its own safety
  envelope in its manifest, and the Settable's limits are read from that. A
  limit written down in two places is a limit that will disagree with itself.
* **Which instruments to connect is the caller's choice.** `include=` names
  them (module keys or discovery slugs), and only those services need to be
  running.

Usage:

    from scan_core.lab import build_lab_registry
    reg, lab = build_lab_registry(include=("lockin", "stage"), prefix=True)
    try:
        ds = run(recipe, reg)
    finally:
        lab.close()
"""

from __future__ import annotations

from suite_common import discover

from .errors import Fault
from .instrument import Instrument, InstrumentError, status_problem
from .manifest import describe_or_none, register_manifest
from .registry import Registry


# Which modules exist and where they listen is NOT typed here any more. It
# comes from module discovery (suite-common): every modules/<category>/<folder>/module.toml, plus
# this PC's suite_local.json, which the launcher writes. So a port changed in
# the launcher is the port scan-core dials, and a new module needs no edit here.

def known_ports(root=None) -> dict[str, tuple[int, int]]:
    """{key: (cmd, pub)} for every LOCAL module, with this PC's overrides."""
    # not the INSTANCES (a second copy, "scope#dwf"): they share the key and
    # would overwrite the module itself here; they are reached by slug instead
    return {m.key: (m.cmd, m.pub) for m in discover(root).modules
            if not m.remote and not m.instance}


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
        never settles (a feedback loop that cannot reach its target, a
        controller with no calibration) makes Abort look dead for the length of the timeout.
        Pass None to clear it.
        """
        for inst in self.instruments.values():
            inst.should_abort = should_abort

    # ---- faults: can the readings be trusted right now? -------------------

    def faults(self, names=None) -> list:
        """[Fault(name, message)] for every instrument that cannot be trusted now.

        `names` limits the check to those connections (the ones a scan uses);
        None = all of them. An instrument is reported when

          * its service is dead or silent -- `status()` raises once the PUB
            cache is older than `stale_after_s` and a direct request fails too;
          * its status carries a non-empty `hw_error` (the last hardware read
            failed: the published values are not the instrument's);
          * its status carries a non-empty `fault` (the module says measuring
            now gives wrong data and a person may be needed -- e.g. a
            tracker lost its reference). Such a module may LATCH it until `clear_fault`.

        Never raises: the whole point is to be asked at every scan point, and
        a check that can itself blow up would turn a paused scan into a
        crashed one. An empty list means "go ahead".
        """
        out = []
        for name, inst in list(self.instruments.items()):
            if names is not None and name not in names:
                continue
            label = module_prefix(inst)
            try:
                st = inst.status()
            except Exception as exc:
                out.append(Fault(label, f"not answering ({exc})"))
                continue
            problem = status_problem(st)
            if problem:
                out.append(Fault(label, problem))
        return out

    def _by_label(self, label: str):
        for name, inst in self.instruments.items():
            if label in (name, module_prefix(inst)):
                return inst
        return None

    def can_clear_fault(self, label: str) -> bool:
        """True when this module's `describe` offers an action `clear_fault`.

        A module that LATCHES its fault (until someone has looked at the
        cause) offers it; one whose fault clears by itself does not.
        """
        inst = self._by_label(label)
        manifest = getattr(inst, "manifest", None) or {}
        return any(d.get("kind") == "action" and d.get("id") == "clear_fault"
                   for d in manifest.get("parameters", []))

    def clear_fault(self, label: str) -> dict:
        """Send `clear_fault` to that module. Raises InstrumentError if refused
        (a module refuses while the cause is still there -- an interlock still open)."""
        inst = self._by_label(label)
        if inst is None:
            raise InstrumentError(f"{label}: not connected")
        return inst.command("clear_fault")

    # ---- one scan at a time per instrument (suite_common/control.py) ------

    def claim_scan(self, names, label: str, on_log=None):
        """Claim every named instrument for ONE scan, or none of them.

        Returns a `release()` to call when the scan ends (normally, aborted or
        failed). Raises ScanBusy -- after giving back what it had claimed --
        when another scan (a second suite, here or on another PC) uses one of
        them: nothing has moved yet, so nothing needs undoing. A service that
        predates the claim is reported through `on_log` and left unprotected.
        """
        log = on_log or (lambda m: None)
        claimed = []
        try:
            for name in sorted(set(names)):
                inst = self.instruments.get(name)
                if inst is None or not hasattr(inst, "claim_scan"):
                    continue
                if inst.claim_scan(label):
                    claimed.append(inst)
                else:
                    log(f"{module_prefix(inst)}: this service cannot be claimed for "
                        "one scan (it predates it) -- another scan could drive it too")
        except Exception:
            for inst in claimed:
                inst.release_scan()
            raise

        def release():
            for inst in claimed:
                inst.release_scan()
        return release

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

          * a positioner's travel can shrink when an axis switches to
            closed loop
          * a software leash armed around a datum can REPLACE the travel clamp
          * a controller's range can BE its loaded calibration, so measuring
            or loading one changes it

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
            # ONE module that does not answer must not stop the others being
            # refreshed. status() RAISES for a silent service (since 2026-09-28),
            # and that used to leave this loop: the camera was never re-read,
            # a scan to scan_ix 48 passed validation against the camera's OLD
            # limits, and the camera (48 points: 0..47) clamped it -- the run
            # then sat out a 60 s timeout (2026-10-02).
            try:
                live_rev = (inst.status() or {}).get("describe_rev")
            except Exception as exc:
                warn(f"{name}: no status ({exc}); its limits may be stale")
                continue
            # Compare with the revision whose limits are IN THE REGISTRY -- not
            # with inst.manifest. Other readers refresh inst.manifest too (the
            # Control tab when it re-clamps its own widgets, the clamp guard),
            # and then the two revisions matched, this loop skipped, and the
            # registry kept the OLD limits for good: the Scan tab offered scan
            # point 0..48 for a 45-point array (roadmap bug, 2026-10-02).
            applied = getattr(inst, "limits_rev", cached.get("revision"))
            if live_rev is None or live_rev == applied:
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
            inst.limits_rev = fresh.get("revision")

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


# ------------------------- the lab registry ---------------------------------

def build_lab_registry(include, host: str = "localhost",
                       timeout_ms: int = 3000, ports: dict | None = None,
                       prefix: bool = False,
                       on_warn=None, endpoints: dict | None = None):
    """Connect to the named services and return (Registry, Lab).

    `include` is REQUIRED: the module keys (or discovery slugs) to connect.
    There is no default instrument -- the suite does not assume any lab's rig.
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
    `position`. `on_warn` receives non-fatal problems (an unrecognised settle
    policy, a parameter that cannot be scanned) instead of them vanishing.
    A module that does not answer `describe` raises InstrumentError.
    """
    if isinstance(include, str):
        include = (include,)          # "lockin" is one module, not six letters
    reg = Registry()
    lab = Lab()
    ports = ports or {}
    endpoints = endpoints or {}
    warn = on_warn or (lambda m: None)
    # Which connection owns each parameter/action id, so a scan's fault check
    # asks only about the instruments that scan USES: a module that happens to
    # be connected but plays no part must not pause somebody's measurement.
    owner: dict[str, str] = {}

    def _known_ids():
        return ({p.id for p in reg.settables()} | {p.id for p in reg.gettables()}
                | {a.id for a in reg.actions()})

    def fault_check(ids=None):
        if ids is None:
            return lab.faults()
        names = {owner[i] for i in ids if i in owner}
        return lab.faults(names) if names else []

    def scan_claim(ids, label, on_log=None):
        # the instruments THIS scan uses, as for the fault check
        return lab.claim_scan({owner[i] for i in ids if i in owner}, label, on_log)

    def snapshot(**kw):
        # EVERY connected instrument, not only the ones a scan uses: the
        # setting that spoils a map is usually on one nobody thought of
        from .snapshot import include_idn_setting, take_snapshot
        if "include_idn" not in kw:
            # the suite setting snapshot_include_idn, in the suite's root
            kw["include_idn"] = include_idn_setting(getattr(reg, "settings_root", None))
        snap = take_snapshot(lab, **kw)
        _add_parameter_values(snap, reg, owner, lab)
        return snap

    reg.fault_check = fault_check
    reg.scan_claim = scan_claim
    reg.snapshot = snapshot           # engine.run files it with the data
    reg.owner = owner
    try:
        for name in include:
            before = _known_ids()
            _connect_one(reg, lab, name, host, ports, endpoints, timeout_ms,
                         prefix, warn)
            for i in _known_ids() - before:
                owner[i] = name
    except Exception:
        lab.close()          # never leave half-open sockets behind
        raise
    return reg, lab


def _suite_keys() -> set[str]:
    """The module keys this suite knows (discovery), lower case."""
    keys = {"scanserver"}
    try:
        from suite_common import discover
        keys |= {m.key.lower() for m in discover().modules}
    except Exception:
        pass
    return keys


def _check_identity(name: str, manifest: dict) -> None:
    """Refuse a service that is not the module we came for.

    A port that answers is not proof of WHO answers: on 2026-10-05 the lab's
    scan server, moved for a test onto sr830's default ports, was found by
    "follow the launcher" as sr830 and connected as 14 phantom detectors. The
    module's own `describe` says who it is; `name` is the module key or its
    discovery slug "<key>_<host>[_<port>]", so it must start with that key.

    Only a service naming ANOTHER module of this suite (or the scan server) is
    refused: a service the suite does not know (a test's fake, a module from
    elsewhere) is taken as what it was asked to be, as before."""
    mod = str(manifest.get("module") or "").lower()
    want = name.lower()
    if not mod or want == mod or want.startswith(mod + "_"):
        return
    if mod in _suite_keys():
        raise RuntimeError(
            f"{name}: the service on that port is '{manifest.get('module')}', "
            f"not {name} -- two services share a port number (check the "
            f"Ports of both cards in Mission Control)")


def _connect_one(reg, lab, name, host, ports, endpoints, timeout_ms, prefix, warn):
    """Connect one service and register its parameters (build_lab_registry)."""
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

    # The module's own self-description is the ONLY source. Everything a
    # Parameter needs -- limits, verb, status key, settle rule -- lives in the
    # module that owns the hardware; a second copy here would disagree with it.
    manifest = describe_or_none(inst)
    if manifest is None:
        raise InstrumentError(
            f"{name}: does not answer `describe`, so scan-core cannot know its "
            f"parameters. Every module must describe itself -- see "
            f"INSTRUMENT_MODULE_GUIDE.md section 6b.")
    _check_identity(name, manifest)
    inst.manifest = manifest
    register_manifest(reg, inst, manifest, prefix=prefix,
                      on_warn=warn, module_name=inst.alias)
    # the revision whose limits the registry now holds (refresh_stale)
    inst.limits_rev = manifest.get("revision")


def _add_parameter_values(snap, reg, owner, lab):
    """Add `values` = {parameter id: value} to each instrument's snapshot entry.

    Why (lab PC, 2026-10-05): the snapshot holds a module's raw get_config /
    status, whose keys (`sensor.wavelength_nm`) are not the names a person
    scans with (`meter.wavelength`), so the catalogue's `where meter.wavelength
    > 600` found nothing. Each scalar parameter's current value (the readback
    or cached status, never a new acquisition) under its SCAN name fixes that.
    Never raises; arrays and unreadable values are skipped.
    """
    import math
    slug_of = {}
    for name, inst in getattr(lab, "instruments", {}).items():
        try:
            slug_of[name] = module_prefix(inst)
        except Exception:
            slug_of[name] = name
    for pid, name in dict(owner or {}).items():
        slug = slug_of.get(name)
        entry = snap.get(slug)
        p = reg.get(pid)
        if entry is None or p is None or getattr(p, "axes", None):
            continue
        try:
            v = p.get()
        except Exception:
            continue
        if isinstance(v, bool) or (isinstance(v, (int, float)) and math.isfinite(v)):
            pass
        elif isinstance(v, str) and len(v) <= 200:
            pass
        else:
            continue
        short = pid[len(slug) + 1:] if pid.startswith(slug + ".") else pid
        entry.setdefault("values", {})[short] = v
