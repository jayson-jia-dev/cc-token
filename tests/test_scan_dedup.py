#!/usr/bin/env python3
"""Regression test for scan() msg.id deduplication.

Claude Code can re-log the same Anthropic API response multiple times
within a single JSONL (seen on session resume / continue — 40-60% of
rows in long sessions are duplicates). Before the fix, scan() summed
every row blindly, inflating cost by ~44% on real machines.

This test builds a deterministic fixture with known duplicates and
asserts that scan() counts each msg.id exactly once. Run with:

    python3 tests/test_scan_dedup.py
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


def make_row(msg_id, model="claude-sonnet-4-5", inp=1000, out=500, cw=0, cr=0,
             cw_5m=None, cw_1h=None, ts="2026-04-15T10:00:00Z"):
    usage = {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
    }
    if cw_5m is not None or cw_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cw_5m or 0,
            "ephemeral_1h_input_tokens": cw_1h or 0,
        }
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {"id": msg_id, "model": model, "usage": usage},
    })


class ScanDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-dedup-test-")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.projects, "fakeproj"))
        self.jsonl = os.path.join(self.projects, "fakeproj", "session.jsonl")

        self.mod = load_plugin()
        # Redirect plugin to fixture tree + disable cache reuse
        self.mod.CLAUDE_DIR = self.tmp
        self.mod.SCAN_CACHE_FILE = Path(self.tmp) / ".scan-cache.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rows):
        with open(self.jsonl, "w") as f:
            for r in rows:
                f.write(r + "\n")

    def test_unique_ids_counted_once(self):
        self._write([
            make_row("msg_a", inp=1000, out=500),
            make_row("msg_b", inp=2000, out=1000),
            make_row("msg_c", inp=500,  out=200),
        ])
        s = self.mod.scan()
        self.assertEqual(s["inp"], 3500, "input_tokens should sum across 3 unique msgs")
        self.assertEqual(s["out"], 1700)

    def test_duplicate_ids_counted_once(self):
        # msg_a appears 3 times (resume scenario), msg_b appears 2 times
        self._write([
            make_row("msg_a", inp=1000, out=500),
            make_row("msg_a", inp=1000, out=500),  # dup from resume
            make_row("msg_b", inp=2000, out=1000),
            make_row("msg_a", inp=1000, out=500),  # dup again
            make_row("msg_b", inp=2000, out=1000),  # dup
        ])
        s = self.mod.scan()
        # Expect only first occurrence of each id counted:
        # msg_a: 1000 + 500 = 1500 tokens (input+output)
        # msg_b: 2000 + 1000 = 3000 tokens
        self.assertEqual(s["inp"], 3000, f"input should be 1000+2000, got {s['inp']}")
        self.assertEqual(s["out"], 1500, f"output should be 500+1000, got {s['out']}")

    def test_duplicates_across_files_counted_once(self):
        # Less common in practice (cross-file dup was 0 on real machine)
        # but msg.id is globally unique per Anthropic API so dedup must be global.
        os.makedirs(os.path.join(self.projects, "otherproj"))
        other = os.path.join(self.projects, "otherproj", "session.jsonl")
        self._write([make_row("shared_id", inp=1000, out=500)])
        with open(other, "w") as f:
            f.write(make_row("shared_id", inp=1000, out=500) + "\n")
            f.write(make_row("unique_id", inp=500, out=200) + "\n")
        s = self.mod.scan()
        # shared_id once + unique_id once
        self.assertEqual(s["inp"], 1500)
        self.assertEqual(s["out"], 700)

    def test_no_usage_rows_skipped(self):
        # Rows without message.usage must not throw and must not be counted
        self._write([
            make_row("msg_a", inp=1000, out=500),
            json.dumps({"type": "user", "timestamp": "2026-04-15T10:00:00Z", "message": {"content": "hi"}}),
            json.dumps({"type": "assistant", "timestamp": "2026-04-15T10:00:00Z", "message": {"id": "msg_b"}}),  # no usage
        ])
        s = self.mod.scan()
        self.assertEqual(s["inp"], 1000)
        self.assertEqual(s["out"], 500)

    def test_session_cost_matches_dedup_total(self):
        # The per-session sess_cost accumulator must use the same dedup —
        # otherwise daily.sessions count and session-level breakdowns drift.
        self._write([
            make_row("m1", inp=1000, out=500),
            make_row("m1", inp=1000, out=500),  # dup within session
            make_row("m2", inp=2000, out=1000),
        ])
        s = self.mod.scan()
        # Session cost should equal total cost (single-session fixture, all dedup)
        daily = dict(s["daily"])
        total_daily_cost = sum(v["cost"] for v in daily.values())
        self.assertAlmostEqual(total_daily_cost, s["cost"], places=4,
            msg="daily cost must match total cost — both go through the same dedup path")

    def test_cache_ttl_split_prices_5m_and_1h_differently(self):
        # Sonnet: input=3, cw_5m=3.75, cw_1h=6. 1M tokens of 5m → $3.75,
        # 1M tokens of 1h → $6. Flat 1h fallback would wrongly price 5m at $6.
        self._write([
            make_row("m1", model="claude-sonnet-4-6", inp=0, out=0,
                     cw=1_000_000, cw_5m=1_000_000, cw_1h=0),
            make_row("m2", model="claude-sonnet-4-6", inp=0, out=0,
                     cw=1_000_000, cw_5m=0, cw_1h=1_000_000),
        ])
        s = self.mod.scan()
        # Expected: 3.75 + 6 = 9.75
        self.assertAlmostEqual(s["cost"], 9.75, places=4,
            msg=f"5m×$3.75 + 1h×$6 should equal $9.75, got ${s['cost']:.4f}")

    def test_cache_ttl_missing_falls_back_to_1h(self):
        # Old JSONL without cache_creation nested object: flat cw priced at 1h.
        self._write([
            make_row("m1", model="claude-sonnet-4-6", inp=0, out=0, cw=1_000_000),
        ])
        s = self.mod.scan()
        self.assertAlmostEqual(s["cost"], 6.0, places=4,
            msg=f"flat 1M cw tokens with no TTL split should price at $6 (1h), got ${s['cost']:.4f}")


class RemoteLoadConsistencyTest(unittest.TestCase):
    """Regression tests for v1.4.2 R-1/R-2 fix: load_remotes() no longer
    recalculates total_cost. daily.cost vs total_cost must stay self-
    consistent — whatever home-mac wrote is what's loaded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-remote-test-")
        self.sync_machines = os.path.join(self.tmp, "machines")
        self.remote_dir = os.path.join(self.sync_machines, "fake-mac")
        os.makedirs(self.remote_dir)
        self.mod = load_plugin()
        self.mod.SYNC_DIR = self.tmp
        # Pretend this process is a different machine so load_remotes
        # actually loads the fake remote (it skips self.MACHINE).
        self.mod.MACHINE = "THIS-IS-NOT-FAKE-MAC"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_remote(self, **fields):
        data = {
            "machine": "fake-mac",
            "session_count": 10,
            "input_tokens": 1000, "output_tokens": 500,
            "cache_write_tokens": 2000, "cache_read_tokens": 5000,
            "total_cost": 42.00,
            "date_range": {"min": "2026-04-10", "max": "2026-04-20"},
            "model_breakdown": {"claude-sonnet-4-6": {"msgs": 10, "tokens": 8500, "cost": 42.00}},
            "daily": {
                "2026-04-10": {"cost": 20.00, "msgs": 5, "tokens": 4250, "sessions": 1},
                "2026-04-20": {"cost": 22.00, "msgs": 5, "tokens": 4250, "sessions": 1},
            },
            "hourly": {"14": 10},
            "projects": {}, "today": {"cost": 0, "msgs": 0, "tokens": 0},
            "daily_models": {}, "daily_hourly": {}, "sessions_by_day": {},
        }
        data.update(fields)
        with open(os.path.join(self.remote_dir, "token-stats.json"), "w") as f:
            json.dump(data, f)

    def test_remote_total_cost_not_recalculated(self):
        # Home-mac's written total_cost must be trusted verbatim.
        self._write_remote(total_cost=42.00)
        remotes = self.mod.load_remotes()
        self.assertEqual(len(remotes), 1)
        self.assertEqual(remotes[0]["cost"], 42.00)

    def test_remote_daily_sum_matches_total_cost(self):
        # After load, sum(daily.cost) must equal total_cost — no silent
        # divergence like the $4.27 gap before v1.4.2.
        self._write_remote()
        remotes = self.mod.load_remotes()
        daily_sum = sum(v.get("cost", 0) for v in remotes[0]["daily"].values())
        self.assertAlmostEqual(daily_sum, remotes[0]["cost"], places=2)

    def test_remote_fields_normalized_to_scan_shape(self):
        # load_remotes must expose inp/out/cw/cr (not just the long
        # input_tokens/etc. names) so merge code doesn't need OR-tricks.
        self._write_remote()
        r = self.mod.load_remotes()[0]
        self.assertEqual(r["inp"], 1000)
        self.assertEqual(r["out"], 500)
        self.assertEqual(r["cw"], 2000)
        self.assertEqual(r["cr"], 5000)
        self.assertEqual(r["sessions"], 10)
        self.assertEqual(r["d_min"], "2026-04-10")
        self.assertEqual(r["d_max"], "2026-04-20")


class UserLevelDedupTest(unittest.TestCase):
    """Regression for v1.4.2 U-1 fix: calc_user_level's median session
    length must not be inflated by session-resume duplicates."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-level-test-")
        self.projects = os.path.join(self.tmp, "projects", "testproj")
        os.makedirs(self.projects)
        self.jsonl = os.path.join(self.projects, "session.jsonl")
        self.mod = load_plugin()
        self.mod.CLAUDE_DIR = self.tmp
        # Kill level cache so each call recomputes
        self.mod.LEVEL_CACHE_FILE = Path(self.tmp) / ".level_cache.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_level_uses_deduped_session_length(self):
        # 5 unique msgs + 50 duplicates → raw cnt = 55 (tier >=50 → 10pt
        # msg-bucket), dedup cnt = 5 (tier <10 → 0pt msg-bucket).
        # Density bonus is identical either way, so the whole difference
        # (10 → 0) comes from dedup.
        rows = [make_row(f"unique_{i}") for i in range(5)]
        for i in range(50):
            rows.append(make_row(f"unique_{i % 5}"))  # duplicates
        with open(self.jsonl, "w") as f:
            for r in rows:
                f.write(r + "\n")
        score, lvl, details, _ = self.mod.calc_user_level()
        # With dedup: msg-tier 0 + density bonus (≤4) = usage ≤ 4
        # Without dedup: msg-tier 10 + density bonus (≤4) = usage ≥ 10
        self.assertLessEqual(details["usage"], 4,
            msg=f"dedup'd 5-msg session should score usage ≤4, got {details['usage']}")


class NotifyDedupTest(unittest.TestCase):
    """Regression for v1.4.5 notification spam fix.

    Before: jumping 0→100% util in one cycle fired 80/95/100/burn = 4
    pushes; each 5h window rollover re-fired the whole set. The user
    showed a screenshot of 8 pushes in 10 min. New semantics must
    guarantee: one notification per limit per escalation.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-notify-test-")
        self.mod = load_plugin()
        self.mod.NOTIFY_STATE_FILE = Path(self.tmp) / "notify.json"
        # CFG defaults notifications=True via CFG.get default, fine.
        self.mod.CFG = {"notifications": True}
        # Intercept _notify so we can assert count without invoking osascript
        self.pushes = []
        self.mod._notify = lambda title, msg: self.pushes.append((title, msg))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _usage(self, util, reset=None):
        # Default reset: 2 hours in the future so burn-rate math is well-defined
        # (within the 5h window constraint).
        if reset is None:
            from datetime import datetime, timedelta
            reset = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
        return {"five_hour": {"utilization": util, "resets_at": reset}}

    def test_jump_zero_to_100_fires_once(self):
        self.mod.check_and_notify(self._usage(100))
        self.assertEqual(len(self.pushes), 1, f"expected 1 push, got {len(self.pushes)}: {self.pushes}")
        self.assertIn("🛑", self.pushes[0][0])

    def test_repeat_at_same_level_no_refire(self):
        self.mod.check_and_notify(self._usage(100))
        self.mod.check_and_notify(self._usage(100))
        self.mod.check_and_notify(self._usage(100))
        self.assertEqual(len(self.pushes), 1, "same-level checks must not re-fire")

    def test_window_rollover_no_refire(self):
        # Window A: util 100, resets 19:00. Fires once.
        self.mod.check_and_notify(self._usage(100, reset="2026-04-20T19:00Z"))
        # Window B opens (new reset time). Util still 100% (heavy user).
        # Under old logic: fires again. Under fix: no fire (user already knows).
        self.mod.check_and_notify(self._usage(100, reset="2026-04-21T00:00Z"))
        self.assertEqual(len(self.pushes), 1,
            f"rollover should not re-fire, got {len(self.pushes)}")

    def test_drop_and_recross_fires_again(self):
        self.mod.check_and_notify(self._usage(100))
        self.assertEqual(len(self.pushes), 1)
        # Util drops to 0 (new session starts or reset cleared quota)
        self.mod.check_and_notify(self._usage(0))
        # Now ramps to 100 again — SHOULD re-fire
        self.mod.check_and_notify(self._usage(100))
        self.assertEqual(len(self.pushes), 2,
            "crossing again after dropping below 80 should re-fire")

    def test_escalation_through_tiers(self):
        # User ramps 75 → 85 → 96 → 100 across four checks. Expected:
        # one notification per tier crossing PLUS optionally one burn
        # notification if the ramp is steep enough to predict <30min to full.
        # 75% is below all tiers (no tier notif, burn math isn't critical yet).
        # 85% crosses 80 → 1 tier notif. 96% crosses 95 → 1 tier notif AND
        # burn fires (util=96% with 2h remaining makes min_to_full ~7min).
        # 100% crosses 100 → 1 tier notif (burn suppressed at ≥100 tier).
        # Total: 4 notifications — distinct escalation events, not spam.
        self.mod.check_and_notify(self._usage(75))
        self.mod.check_and_notify(self._usage(85))
        self.mod.check_and_notify(self._usage(96))
        self.mod.check_and_notify(self._usage(100))
        # Each tier crossing produces exactly ONE tier notif (no 80+95+100
        # firing at once). Kind matters more than count here.
        tier_pushes = [p for p in self.pushes if p[0].startswith(("⚠️", "⛔", "🛑"))]
        self.assertEqual(len(tier_pushes), 3,
            f"expected exactly 3 tier notifs (80/95/100), got {len(tier_pushes)}: {[p[0] for p in tier_pushes]}")
        # Burn is optional (may or may not fire depending on ramp). What MUST
        # hold: total notifications ≤ 4 (no 80/95/100 dup-firing).
        self.assertLessEqual(len(self.pushes), 4,
            f"too many notifications — looks like tier dup-firing: {[p[0] for p in self.pushes]}")

    def test_burn_suppressed_when_already_blocked(self):
        # Contrive a burn-rate scenario: 90% util, reset 5min away → well
        # under 30min to full. But simultaneously hit 100% → burn should
        # be skipped because limit_blocked already fires.
        from datetime import datetime, timedelta, timezone
        reset = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat()
        self.mod.check_and_notify({"five_hour": {"utilization": 100, "resets_at": reset}})
        # Exactly 1 notification (limit_blocked) — not 2 (blocked + burn)
        self.assertEqual(len(self.pushes), 1, f"burn must not fire at 100%, got {self.pushes}")
        self.assertIn("🛑", self.pushes[0][0])


class SubagentsRecursionTest(unittest.TestCase):
    """scan() must recurse into projects/<proj>/<session>/subagents/*.jsonl.

    Measurement on the author's 2-machine fleet found 8,706 msg.ids living
    ONLY in subagents/ files (never in the project root), representing ~50%
    of real cumulative cost plus entire missing days (e.g. 2026-03-02 had
    no project-root JSONL at all — only a subagents/agent-acompact-*.jsonl).
    Root-only scan silently under-reported and made those days unrecoverable
    since save_sync() overwrites iCloud with the incomplete view.

    These tests lock down the recursion behavior so subagents-only data
    can never silently vanish again.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-subagents-test-")
        self.projects = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.projects, "fakeproj")
        # Typical layout: <proj>/<session-uuid>/subagents/<agent>.jsonl
        self.session_dir = os.path.join(self.proj_dir, "session-abc")
        self.sub_dir = os.path.join(self.session_dir, "subagents")
        os.makedirs(self.sub_dir)
        self.root_jsonl = os.path.join(self.proj_dir, "main.jsonl")
        self.sub_jsonl  = os.path.join(self.sub_dir, "agent-xyz.jsonl")

        self.mod = load_plugin()
        self.mod.CLAUDE_DIR = self.tmp
        self.mod.SCAN_CACHE_FILE = Path(self.tmp) / ".scan-cache.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, rows):
        with open(path, "w") as f:
            for r in rows:
                f.write(r + "\n")

    def test_subagent_only_ids_counted(self):
        # Root has one msg, subagent has another — both must contribute.
        self._write(self.root_jsonl, [make_row("root_msg", inp=1000, out=500)])
        self._write(self.sub_jsonl,  [make_row("sub_msg",  inp=2000, out=1000)])
        s = self.mod.scan()
        self.assertEqual(s["inp"], 3000, "inp should include both root + subagent")
        self.assertEqual(s["out"], 1500)

    def test_subagent_only_day_recovered(self):
        # Exactly the regression that motivated this fix: a whole day lives
        # ONLY in subagents/, so root-only scan produced an empty daily entry
        # and save_sync() overwrote iCloud with no 2026-03-02 data.
        self._write(self.root_jsonl, [
            make_row("r1", ts="2026-04-15T10:00:00Z", inp=1000, out=500),
        ])
        self._write(self.sub_jsonl, [
            make_row("s1", ts="2026-03-02T10:00:00Z", inp=500, out=200),
        ])
        s = self.mod.scan()
        self.assertIn("2026-03-02", s["daily"],
                      "subagent-only day must appear in daily breakdown")
        self.assertGreater(s["daily"]["2026-03-02"]["msgs"], 0)

    def test_root_and_subagent_overlap_deduped(self):
        # Same msg.id in BOTH root and subagent file (happens when a subagent
        # response gets re-logged into the parent session). Must count once.
        self._write(self.root_jsonl, [make_row("dup_id", inp=1000, out=500)])
        self._write(self.sub_jsonl,  [make_row("dup_id", inp=1000, out=500)])
        s = self.mod.scan()
        # First-wins: only one row counted
        self.assertEqual(s["inp"], 1000)
        self.assertEqual(s["out"], 500)

    def test_subagent_contributes_to_project_totals(self):
        # Per-project rollup must include subagents so the Top Projects
        # panel and dashboard reflect real usage per project.
        self._write(self.root_jsonl, [make_row("r1", inp=1000, out=500)])
        self._write(self.sub_jsonl,  [make_row("s1", inp=2000, out=1000)])
        s = self.mod.scan()
        # Project name comes from the project dir basename ('fakeproj')
        self.assertIn("fakeproj", s["projects"])
        p = s["projects"]["fakeproj"]
        self.assertEqual(p["msgs"], 2, "project msg count spans root + subagent")
        self.assertEqual(p["tokens"], 1500 + 3000)

    def test_deeply_nested_subagent_picked_up(self):
        # Claude Code occasionally nests subagents (a subagent spawns another).
        # Make sure the recursive glob catches any depth.
        deep = os.path.join(self.sub_dir, "nested", "deeper")
        os.makedirs(deep)
        self._write(os.path.join(deep, "x.jsonl"), [make_row("deep_id", inp=500, out=100)])
        s = self.mod.scan()
        self.assertGreaterEqual(s["inp"], 500,
                                "deeply nested JSONL inside subagents/ must be scanned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
