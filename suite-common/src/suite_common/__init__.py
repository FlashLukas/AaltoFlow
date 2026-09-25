"""suite_common: what the AaltoFlow suite needs to know about its own modules.

    from suite_common import discover
    found = discover()               # every <folder>/module.toml + remote services
    for m in found.modules:
        print(m.id, m.name, m.host, m.cmd)

Used by mission-control (the launcher) and scan-core. Standard library only.
"""

from .catalog import (InstallPlan, ModuleSource, build_catalog, catalog_text,
                      env_steps, install, is_lab_data, plan_install, search)
from .modules import (CATEGORIES, ENDPOINTS_ENV, PRODUCT, ROOT_ENV, getenv, set_setup_name,
                      setup_name, title)
from .modules import (Discovery, ManifestError, ModuleSpec, add_remote,
                      default_root, discover, endpoints_json, get_setting,
                      gui_args, load_local, port_conflicts, probe,
                      remove_remote, save_local, service_args, set_ports,
                      set_real, set_setting, start_order)

__all__ = [
    "Discovery", "ManifestError", "ModuleSpec", "add_remote", "default_root",
    "discover", "endpoints_json", "get_setting", "gui_args", "load_local",
    "port_conflicts", "probe", "remove_remote", "save_local", "service_args",
    "set_ports", "set_real", "set_setting", "start_order",
    "ENDPOINTS_ENV", "PRODUCT", "ROOT_ENV", "getenv", "set_setup_name",
    "setup_name", "title",
    "CATEGORIES", "InstallPlan", "ModuleSource", "build_catalog", "catalog_text",
    "env_steps", "install", "is_lab_data", "plan_install", "search",
]
