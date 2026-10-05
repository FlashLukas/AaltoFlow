"""suite_common: what the AaltoFlow suite needs to know about its own modules.

    from suite_common import discover
    found = discover()               # every modules/<category>/<folder>/module.toml + remotes
    for m in found.modules:
        print(m.id, m.name, m.host, m.cmd)

Used by mission-control (the launcher) and scan-core. Standard library only.
"""

from .catalog import (InstallPlan, ModuleSource, build_catalog, catalog_text,
                      env_steps, install, is_lab_data, plan_install, search)
from .modules import (CATEGORIES, COORDINATOR_KEYS, ENDPOINTS_ENV, MODULES_DIR, PRODUCT,
                      ROOT_ENV, SUITE_PROJECTS, getenv, is_legacy_location,
                      is_suite_project, manifest_paths, module_home, rel_to_root,
                      set_setup_name, setup_name, title)
from .modules import (Discovery, ManifestError, ModuleSpec, add_remote,
                      default_root, discover, endpoints_json, get_setting,
                      gui_args, load_local, port_conflicts, probe,
                      remove_remote, save_local, service_args, set_ports,
                      set_address, set_real, set_setting, start_order)
from .settings_bundle import (ImportPlan, apply_import, export_bundle,
                              read_bundle)

__all__ = [
    "Discovery", "ManifestError", "ModuleSpec", "add_remote", "default_root",
    "discover", "endpoints_json", "get_setting", "gui_args", "load_local",
    "port_conflicts", "probe", "remove_remote", "save_local", "service_args",
    "set_address", "set_ports", "set_real", "set_setting", "start_order",
    "ENDPOINTS_ENV", "PRODUCT", "ROOT_ENV", "getenv", "set_setup_name",
    "setup_name", "title", "MODULES_DIR", "manifest_paths", "module_home",
    "is_legacy_location", "rel_to_root", "is_suite_project", "SUITE_PROJECTS",
    "COORDINATOR_KEYS",
    "CATEGORIES", "InstallPlan", "ModuleSource", "build_catalog", "catalog_text",
    "env_steps", "install", "is_lab_data", "plan_install", "search",
    "ImportPlan", "apply_import", "export_bundle", "read_bundle",
]
