"""The module's own measurement choices survive a restart (Blue Box, 2026-11-11).

After a restart IFBW was kept (the analyser holds it) but the S-parameter went
back to what S2VNA's screen showed, "also measure" to empty and averages to 1.
Now S-parameter, extra S-parameters and averages are remembered in the
service's .ini and win over the analyser's screen at start.
"""

from __future__ import annotations

import configparser

from vna.config import Config, saved_choices
from vna.sim_system import build_sim_system


def _brain(path, screen_sparam="S11"):
    cfg = Config.load(str(path)) if path.exists() else Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    # the analyser's screen shows `screen_sparam` (what a stop hands back)
    v, _ = build_sim_system(cfg, realtime=False, seed=3,
                            state={"sparam": screen_sparam, "ifbw_Hz": 500.0})
    v.choices_path = str(path)
    v.start(run=False)
    return v


def test_choices_are_written_and_win_over_the_analysers_screen_at_start(tmp_path):
    ini = tmp_path / "vna.ini"
    ini.write_text("[field]\nsource = manual\nmanual_mt = 0.0\n", encoding="utf-8")
    v = _brain(ini)
    try:
        assert v.cfg.sweep.sparam == "S11", "nothing remembered yet: adopt the screen"
        v.set_sparam("S21")
        v.set_extra_sparams("S11")
        v.set_averages(2)
    finally:
        v.shutdown()
    p = configparser.ConfigParser()
    p.read(ini, encoding="utf-8")
    assert p["sweep"]["sparam"] == "S21" and p["sweep"]["extra_sparams"] == "S11"
    assert p["sweep"]["averages"] == "2"
    assert p["field"]["source"] == "manual", "the rest of the file is kept"
    assert saved_choices(ini) == {"sparam", "extra_sparams", "averages"}

    v2 = _brain(ini, screen_sparam="S11")       # restart; S2VNA shows S11 again
    try:
        sw = v2.cfg.sweep
        assert (sw.sparam, sw.extra_sparams, sw.averages) == ("S21", "S11", 2)
        assert sw.ifbw_Hz == 500.0, "what the analyser holds is still adopted"
        assert v2.measured_sparams() == ("S21", "S11")
    finally:
        v2.shutdown()


def test_without_a_choices_file_nothing_is_written(tmp_path):
    cfg = Config()
    cfg.field.source = "manual"
    v, _ = build_sim_system(cfg, realtime=False, seed=3)
    v.start(run=False)
    try:
        v.set_averages(3)                       # choices_path None: tests, GUI sim
    finally:
        v.shutdown()
    assert list(tmp_path.iterdir()) == []
