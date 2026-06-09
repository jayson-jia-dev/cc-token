#!/usr/bin/env python3
"""Regression tests for load_remotes() schema normalization.

save_sync() writes {total_cost, session_count, input_tokens, ...} for
cross-machine JSON compatibility. Local scan() keeps {cost, sessions,
inp, ...} for efficient in-process aggregation. load_remotes() bridges
the two — if this alias mapping drifts, the menu bar shows $0 for every
remote machine (the v3 save_sync regression fixed in 684783c).

Covers:
- total_cost → cost alias (and same for sessions/inp/out/cw/cr)
- missing optional fields get {} defaults so merge code doesn't crash
- corrupt JSON in one machine's file does not kill the whole load
- own-machine file is skipped (self-import would double-count)

Run with:  python3 tests/test_remotes_normalize.py
"""
import importlib.util
import json
import os
import sys
import tempfile
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


class LoadRemotesTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.tmp = tempfile.mkdtemp(prefix="cc-remotes-test-")
        # Rewire SYNC_DIR + MACHINE to a fixture location
        self.mod.SYNC_DIR = self.tmp
        self.mod.MACHINE = "test-host-local"
        self.machines_dir = os.path.join(self.tmp, "machines")
        os.makedirs(self.machines_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_remote(self, host, payload):
        d = os.path.join(self.machines_dir, host)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "token-stats.json"), "w") as f:
            json.dump(payload, f)

    def test_aliases_total_cost_to_cost(self):
        self._write_remote("host-a", {
            "machine": "host-a",
            "total_cost": 42.5,
            "session_count": 10,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_write_tokens": 2000,
            "cache_read_tokens": 3000,
            "date_range": {"min": "2026-04-01", "max": "2026-04-15"},
            "model_breakdown": {"Opus 4.7": {"msgs": 5, "cost": 40.0, "tokens": 100}},
        })
        remotes = self.mod.load_remotes()
        self.assertEqual(len(remotes), 1)
        r = remotes[0]
        self.assertEqual(r["cost"], 42.5)
        self.assertEqual(r["sessions"], 10)
        self.assertEqual(r["inp"], 1000)
        self.assertEqual(r["out"], 500)
        self.assertEqual(r["cw"], 2000)
        self.assertEqual(r["cr"], 3000)
        self.assertEqual(r["d_min"], "2026-04-01")
        self.assertEqual(r["d_max"], "2026-04-15")
        self.assertIn("Opus 4.7", r["models"])

    def test_missing_optional_fields_default_to_empty(self):
        # An older remote that didn't write v3 fields (daily_models, heatmap,
        # sessions_by_day) must still be usable — merge code calls .get on
        # these, but the setdefault() path in load_remotes() also primes
        # daily/hourly/projects/today so consumers can assume they exist.
        self._write_remote("host-b", {"machine": "host-b", "total_cost": 1.0})
        remotes = self.mod.load_remotes()
        r = remotes[0]
        for k in ("daily", "hourly", "projects", "today",
                  "daily_models", "daily_hourly", "sessions_by_day"):
            self.assertIn(k, r, f"{k} must be pre-populated by normalizer")
        # today has a full shape
        self.assertEqual(r["today"], {"cost": 0, "msgs": 0, "tokens": 0})

    def test_corrupt_remote_does_not_kill_whole_load(self):
        self._write_remote("good", {"machine": "good", "total_cost": 5.0})
        # Write malformed JSON for bad host
        bad_dir = os.path.join(self.machines_dir, "bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "token-stats.json"), "w") as f:
            f.write("{not valid json")
        remotes = self.mod.load_remotes()
        # Good machine loaded; bad skipped silently
        names = [r.get("machine") for r in remotes]
        self.assertIn("good", names)
        self.assertNotIn("bad", names)

    def test_self_machine_is_skipped(self):
        # A remote directory that happens to match MACHINE must be skipped
        # — otherwise load_remotes() double-counts the local machine when
        # main() does [local] + remotes.
        self._write_remote(self.mod.MACHINE, {"machine": self.mod.MACHINE, "total_cost": 99.0})
        self._write_remote("other", {"machine": "other", "total_cost": 1.0})
        remotes = self.mod.load_remotes()
        names = [r.get("machine") for r in remotes]
        self.assertNotIn(self.mod.MACHINE, names)
        self.assertIn("other", names)

    def test_date_range_absent_returns_none(self):
        self._write_remote("host-c", {"machine": "host-c", "total_cost": 1.0})
        r = self.mod.load_remotes()[0]
        # d_min/d_max should exist as None rather than raise KeyError
        self.assertIsNone(r["d_min"])
        self.assertIsNone(r["d_max"])

    def test_no_sync_dir_returns_empty(self):
        self.mod.SYNC_DIR = None
        self.assertEqual(self.mod.load_remotes(), [])

    def test_missing_machines_subdir_returns_empty(self):
        # SYNC_DIR exists but machines/ doesn't (fresh install on a peer)
        empty_sync = tempfile.mkdtemp(prefix="cc-empty-sync-")
        self.mod.SYNC_DIR = empty_sync
        try:
            self.assertEqual(self.mod.load_remotes(), [])
        finally:
            import shutil
            shutil.rmtree(empty_sync, ignore_errors=True)

    def test_v3_fields_preserved(self):
        # Remote wrote v3 heatmap/sessions_by_day — must come through intact
        # (load_remotes should NOT overwrite with {} when the field exists).
        self._write_remote("host-d", {
            "machine": "host-d",
            "total_cost": 1.0,
            "daily_models": {"2026-04-15": {"Opus 4.7": {"cost": 0.5, "msgs": 3}}},
            "daily_hourly": {"0": {"9": 5}},
            "sessions_by_day": {"2026-04-15": [{"project": "x", "cost": 0.5, "msgs": 3, "model": "Opus 4.7"}]},
        })
        r = self.mod.load_remotes()[0]
        self.assertIn("2026-04-15", r["daily_models"])
        self.assertEqual(r["daily_hourly"]["0"]["9"], 5)
        self.assertEqual(r["sessions_by_day"]["2026-04-15"][0]["msgs"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
