r"""gen_components.py -- turn the suite's module.toml manifests into Inno Setup sections.

Run by build_installer.ps1 against the staged copy:

    python gen_components.py <stage-dir> <out-dir>

writes  <out-dir>\components.iss       [Components] [Files] [UninstallDelete]
        <out-dir>\components_code.iss  the SelectedProjects() function

Why generated: the launcher DISCOVERS modules (every folder with a
`module.toml`), so a new module is a folder drop -- see
suite-common/src/suite_common/modules.py. If the installer kept its own list,
adding a module would mean editing it here too, and the two lists would drift.
Now the wizard's checkbox list IS the manifest list: name, description and the
order they appear in all come from each module.toml, exactly like the cards in
mission-control.

Three things are always installed and get no checkbox:
  suite-common     the discovery + per-PC settings package that mission-control
                   and scan-core depend on (a path dependency: `uv sync` fails
                   without the folder next door)
  mission-control  the launcher -- the thing you start
and scan-core is one checkbox of its own, because it is a client of everything
rather than an instrument.

LAB DATA. Some files in a module folder are not code but measurements of the
real rig: a calibration, a tuned .ini. Those are installed only when missing
(`onlyifdoesntexist`) and left behind on uninstall, so upgrading never throws
away an afternoon on the bench. The rule is generic, so a future module gets
the same protection without anyone remembering to add it here:

    <module>\*.ini                  settings tuned on the rig
    <module>\*calibration*.json     e.g. kim's px_calibration.json
    <module>\Calibrations\*         clMag's calibration folder

The rule itself is suite_common.catalog.is_lab_data (see _lab_data_rule): the
same function decides what Mission Control's "Export settings..." saves.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Folders that are part of the suite but are not instruments.
ALWAYS = ["suite-common", "mission-control"]   # component "core", fixed
SCAN = "scan-core"                             # component "scan"


def _lab_data_rule(stage: Path):
    """suite_common.catalog.is_lab_data, imported from the STAGED suite-common.

    One function decides what "lab data" is, and three things use it: this
    installer (install only if missing, keep on uninstall), Mission Control's
    "Add module" updates (never overwrite), and Mission Control's settings
    export/import (settings_bundle.py). A second copy of the rule here could
    drift, and then a backup would miss a file the installer protects. The
    staged tree always contains suite-common (it is a fixed component), and it
    is standard library only, so importing it from here needs nothing installed.
    """
    src = str(stage / "suite-common" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from suite_common.catalog import is_lab_data
    return is_lab_data


def data_globs(module_dir: Path) -> list[str]:
    """Relative patterns inside `module_dir` that hold lab data, not code."""
    is_lab_data = _lab_data_rule(module_dir.parent)
    globs = [f.name for f in sorted(module_dir.iterdir())
             if f.is_file() and is_lab_data(f.name)]
    if (module_dir / "Calibrations").is_dir():
        globs.append(r"Calibrations\*")
    return globs


def files_entry(folder: str, component: str, data: list[str]) -> list[str]:
    """The [Files] lines for one folder: code overwritten, data kept."""
    stage = "{#StageDir}\\" + folder
    lines = []
    if data:
        # Excludes patterns are matched against the path BELOW the source dir,
        # and a leading backslash anchors them to its root -- so "\camera.ini"
        # is the module's own file, not one in a subfolder.
        excl = ",".join("\\" + g for g in data)
        lines.append(f'Source: "{stage}\\*"; DestDir: "{{app}}\\{folder}"; '
                     f'Components: {component}; Excludes: "{excl}"; '
                     f'Flags: ignoreversion recursesubdirs createallsubdirs')
        for g in data:
            dest = f"{{app}}\\{folder}"
            if g.endswith(r"Calibrations\*"):
                dest += r"\Calibrations"
            lines.append(f'Source: "{stage}\\{g}"; DestDir: "{dest}"; '
                         f'Components: {component}; '
                         f'Flags: onlyifdoesntexist uninsneveruninstall recursesubdirs createallsubdirs')
    else:
        lines.append(f'Source: "{stage}\\*"; DestDir: "{{app}}\\{folder}"; '
                     f'Components: {component}; '
                     f'Flags: ignoreversion recursesubdirs createallsubdirs')
    return lines


def main() -> int:
    stage = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    modules = []
    for toml in sorted(stage.glob("*/module.toml")):
        try:
            man = tomllib.loads(toml.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"  SKIPPED {toml.parent.name}: unreadable module.toml ({exc})")
            continue
        m = man.get("module", {})
        key = str(m.get("key") or toml.parent.name)
        modules.append(dict(
            folder=toml.parent.name,
            key=key,
            # Inno component names: lowercase, no spaces. The key is a python
            # identifier by convention, so lowercasing is enough.
            comp="inst\\" + key.lower(),
            name=str(m.get("name") or key),
            desc=str(m.get("description") or ""),
            order=int(m.get("order") or 999),
        ))
    modules.sort(key=lambda d: (d["order"], d["folder"]))
    if not modules:
        print("  ERROR: no module.toml found in the staged tree", file=sys.stderr)
        return 1

    comp_lines = [
        "; GENERATED by gen_components.py from the staged module.toml files.",
        "; Do not edit: rebuild instead.",
        "",
        "[Components]",
        'Name: "core"; Description: "Mission Control launcher (required)"; Types: full custom; Flags: fixed',
        f'Name: "scan"; Description: "Measurement suite + scan engine ({SCAN})"; Types: full',
        'Name: "inst"; Description: "Instrument modules"; Types: full',
    ]
    for m in modules:
        desc = f'{m["name"]} -- {m["desc"]}' if m["desc"] else m["name"]
        desc = desc.replace('"', "'")
        comp_lines.append(f'Name: "{m["comp"]}"; Description: "{desc} ({m["folder"]})"; Types: full')

    comp_lines += ["", "[Files]"]
    for folder in ALWAYS:
        comp_lines += files_entry(folder, "core", data_globs(stage / folder))
    comp_lines += files_entry(SCAN, "scan", data_globs(stage / SCAN))
    for m in modules:
        comp_lines += files_entry(m["folder"], m["comp"], data_globs(stage / m["folder"]))

    comp_lines += [
        "",
        "[UninstallDelete]",
        "; Environments are built after install, so Setup does not know their files.",
        "; Lab data (calibrations, .ini, scan output) is deliberately left behind.",
    ]
    for folder in ALWAYS + [SCAN] + [m["folder"] for m in modules]:
        comp_lines.append(f'Type: filesandordirs; Name: "{{app}}\\{folder}\\.venv"')
    comp_lines.append('Type: files; Name: "{app}\\install_log.txt"')
    comp_lines.append("")

    (out / "components.iss").write_text("\n".join(comp_lines), "utf-8")

    # The list handed to postinstall.ps1: which folders to `uv sync`.
    code = [
        "{ GENERATED by gen_components.py -- do not edit. }",
        "function SelectedProjects(Param: String): String;",
        "begin",
        "  Result := '" + ",".join(ALWAYS) + "';",
        f"  if WizardIsComponentSelected('scan') then Result := Result + ',{SCAN}';",
    ]
    for m in modules:
        code.append(f"  if WizardIsComponentSelected('{m['comp']}') then "
                    f"Result := Result + ',{m['folder']}';")
    code += ["end;", ""]
    (out / "components_code.iss").write_text("\n".join(code), "utf-8")

    print(f"  {len(modules)} modules: " + ", ".join(m["key"] for m in modules))
    for folder in ALWAYS + [SCAN] + [m["folder"] for m in modules]:
        data = data_globs(stage / folder)
        if data:
            print(f"  lab data kept in {folder}: {', '.join(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
