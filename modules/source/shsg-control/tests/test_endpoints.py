"""The launcher's AALTOFLOW_ENDPOINTS points us at the signalhound service.

A port changed in the launcher must reach shsg, or every TG command would go to
the old port from shsg.ini and the TG would simply never react.
"""

import json

from shsg.config import Config
from shsg.endpoints import apply_launcher_endpoints


def test_launcher_endpoint_overrides_the_config():
    cfg = Config()
    env = {"AALTOFLOW_ENDPOINTS": json.dumps({"signalhound": ["localhost", 6587, 6588],
                                              "kim": ["localhost", 5567, 5568]})}
    note = apply_launcher_endpoints(cfg, env)
    hw = cfg.hardware
    assert (hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port) == ("127.0.0.1", 6587, 6588)
    assert "6587" in note
    note.encode("ascii")                       # printed text stays ASCII (gotcha #14)


def test_remote_host_is_kept():
    cfg = Config()
    apply_launcher_endpoints(cfg, {"AALTOFLOW_ENDPOINTS":
                                   json.dumps({"signalhound": ["lab2", 5587, 5588]})})
    assert cfg.hardware.owner_host == "lab2"


def test_old_variable_name_still_works():
    cfg = Config()
    apply_launcher_endpoints(cfg, {"TRMOKE_ENDPOINTS":
                                   json.dumps({"signalhound": ["localhost", 7000, 7001]})})
    assert cfg.hardware.owner_cmd_port == 7000


def test_nothing_to_apply_leaves_the_config_alone():
    for env in ({}, {"AALTOFLOW_ENDPOINTS": json.dumps({"kim": ["localhost", 1, 2]})},
                {"AALTOFLOW_ENDPOINTS": "not json {"}):
        cfg = Config()
        apply_launcher_endpoints(cfg, env)
        assert (cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port) == (5587, 5588)
