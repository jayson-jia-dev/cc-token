#!/usr/bin/env python3
"""Regression tests for _merge_machines_data().

Pre-v1.4.1 main() displayed Daily/Hourly/Projects from local-only scan()
while the header 'Cumulative Cost' was fleet-wide. Two Macs meant two
different numbers on the same menu. _merge_machines_data() unified the
aggregation — this test locks that down.

Covers:
- today/daily/hourly/models/projects all sum across machines
- hourly key coercion (str ↔ int) since local scan uses int, load_remotes uses str
- absent optional fields do not raise
- daily.sessions sums (added in v1.4.1)

Run with:  python3 tests/test_merge_machines.py
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "cc-token.5m.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("cc_token_stats", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cc_token_stats"] = mod
    spec.loader.exec_module(mod)
    return mod


def mk_machine(**kwargs):
    """Build a minimal machine dict with sane defaults."""
    base = {
        "today": {"cost": 0, "msgs": 0, "tokens": 0,
                  "inp": 0, "out": 0, "cw": 0, "cr": 0, "models": {}},
        "daily": {}, "hourly": {}, "models": {}, "projects": {},
    }
    base.update(kwargs)
    return base


class MergeMachinesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()

    def test_today_cost_sums_across_machines(self):
        m1 = mk_machine(today={"cost": 10.5, "msgs": 20, "tokens": 1000,
                               "inp": 400, "out": 600, "cw": 0, "cr": 0, "models": {}})
        m2 = mk_machine(today={"cost": 5.25, "msgs": 10, "tokens": 500,
                               "inp": 200, "out": 300, "cw": 0, "cr": 0, "models": {}})
        today, _, _, _, _ = self.mod._merge_machines_data([m1, m2])
        self.assertEqual(today["cost"], 15.75)
        self.assertEqual(today["msgs"], 30)
        self.assertEqual(today["tokens"], 1500)
        self.assertEqual(today["inp"], 600)

    def test_today_models_merge_by_name(self):
        m1 = mk_machine(today={"cost": 0, "msgs": 0, "tokens": 0,
                               "inp": 0, "out": 0, "cw": 0, "cr": 0,
                               "models": {"Opus 4.7": {"msgs": 5, "cost": 10.0}}})
        m2 = mk_machine(today={"cost": 0, "msgs": 0, "tokens": 0,
                               "inp": 0, "out": 0, "cw": 0, "cr": 0,
                               "models": {"Opus 4.7": {"msgs": 3, "cost": 6.0},
                                          "Sonnet 4.6": {"msgs": 8, "cost": 4.0}}})
        today, _, _, _, _ = self.mod._merge_machines_data([m1, m2])
        self.assertEqual(today["models"]["Opus 4.7"]["msgs"], 8)
        self.assertEqual(today["models"]["Opus 4.7"]["cost"], 16.0)
        self.assertEqual(today["models"]["Sonnet 4.6"]["msgs"], 8)

    def test_daily_sums_cost_msgs_tokens_sessions(self):
        m1 = mk_machine(daily={
            "2026-04-15": {"cost": 10, "msgs": 5, "tokens": 500, "sessions": 2},
        })
        m2 = mk_machine(daily={
            "2026-04-15": {"cost": 3, "msgs": 2, "tokens": 100, "sessions": 1},
            "2026-04-16": {"cost": 7, "msgs": 4, "tokens": 300, "sessions": 3},
        })
        _, daily, _, _, _ = self.mod._merge_machines_data([m1, m2])
        self.assertEqual(daily["2026-04-15"]["cost"], 13)
        self.assertEqual(daily["2026-04-15"]["msgs"], 7)
        self.assertEqual(daily["2026-04-15"]["sessions"], 3)
        self.assertEqual(daily["2026-04-16"]["sessions"], 3)

    def test_hourly_coerces_str_and_int_to_same_bucket(self):
        # Local scan writes hourly as {int: count}, save_sync stringifies for
        # JSON, load_remotes leaves it as {str: count}. Both must merge into
        # the same hour bucket — regression from a prior bug where menu
        # double-counted or dropped remote hours.
        m_local  = mk_machine(hourly={9: 5, 10: 3})         # int keys
        m_remote = mk_machine(hourly={"9": 2, "11": 4})     # str keys
        _, _, hourly, _, _ = self.mod._merge_machines_data([m_local, m_remote])
        self.assertEqual(hourly[9], 7)
        self.assertEqual(hourly[10], 3)
        self.assertEqual(hourly[11], 4)
        # All keys must be int post-merge
        for k in hourly:
            self.assertIsInstance(k, int)

    def test_hourly_non_numeric_key_skipped_not_raised(self):
        # A corrupt remote file could have a non-numeric hour key.
        # Must be skipped silently, not blow up the whole merge.
        m = mk_machine(hourly={"9": 1, "bogus": 99, "10": 2})
        _, _, hourly, _, _ = self.mod._merge_machines_data([m])
        self.assertEqual(hourly, {9: 1, 10: 2})

    def test_models_projects_merge(self):
        m1 = mk_machine(models={"Opus 4.7": {"msgs": 10, "tokens": 1000, "cost": 5.0}},
                        projects={"alpha": {"cost": 3.0, "msgs": 5, "tokens": 500}})
        m2 = mk_machine(models={"Opus 4.7": {"msgs": 5, "tokens": 500, "cost": 2.5},
                                "Sonnet 4.6": {"msgs": 20, "tokens": 2000, "cost": 1.0}},
                        projects={"alpha": {"cost": 1.0, "msgs": 2, "tokens": 100},
                                  "beta":  {"cost": 4.0, "msgs": 8, "tokens": 800}})
        _, _, _, models, projects = self.mod._merge_machines_data([m1, m2])
        self.assertEqual(models["Opus 4.7"]["cost"], 7.5)
        self.assertEqual(models["Opus 4.7"]["msgs"], 15)
        self.assertEqual(models["Sonnet 4.6"]["msgs"], 20)
        self.assertEqual(projects["alpha"]["cost"], 4.0)
        self.assertEqual(projects["beta"]["msgs"], 8)

    def test_empty_machine_list_returns_zero_structs(self):
        today, daily, hourly, models, projects = self.mod._merge_machines_data([])
        self.assertEqual(today["cost"], 0)
        self.assertEqual(today["msgs"], 0)
        self.assertEqual(daily, {})
        self.assertEqual(hourly, {})
        self.assertEqual(models, {})
        self.assertEqual(projects, {})

    def test_missing_optional_fields_do_not_raise(self):
        # load_remotes() normalizer fills defaults but a malformed file
        # might still miss them. Merge must tolerate bare machine dicts.
        sparse = {}
        today, daily, hourly, models, projects = self.mod._merge_machines_data([sparse])
        self.assertEqual(today["cost"], 0)
        self.assertEqual(daily, {})
        self.assertEqual(hourly, {})

    def test_none_valued_fields_treated_as_empty(self):
        # A remote that wrote today=null (older schema ambiguity) must not crash.
        m = {"today": None, "daily": None, "hourly": None, "models": None, "projects": None}
        today, daily, hourly, models, projects = self.mod._merge_machines_data([m])
        self.assertEqual(today["cost"], 0)
        self.assertEqual(hourly, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
