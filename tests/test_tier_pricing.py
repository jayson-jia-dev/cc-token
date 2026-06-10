#!/usr/bin/env python3
"""Regression tests for tier() classification and end-to-end cost math.

tier() routes a model name to one of five PRICING keys (fable, opus_new,
opus_old, sonnet, haiku). Any drift here silently re-prices the entire fleet —
a 5x difference between opus_new ($5 input) and opus_old ($15 input), 3x
between sonnet and opus_new, and 2x between fable ($10) and opus_new. Uncaught
classification bugs were the root cause of v1.4.2's precision pass and the
v1.6.7 Fable 5 launch adaptation (Fable fell through to sonnet → 3.3x undercount).

Run with:  python3 tests/test_tier_pricing.py
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


class TierClassificationTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()

    def test_opus_new_covers_4_5_through_4_7(self):
        # Everything marketed as Opus 4.5+ is on the current (cheaper) pricing.
        for m in (
            "claude-opus-4-5-20250918",
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-opus-4-9-future",
        ):
            self.assertEqual(self.mod.tier(m), "opus_new", f"{m} → opus_new")

    def test_fable_and_mythos_map_to_flagship_tier(self):
        # Fable 5 / Mythos 5 (2026-06-09 launch) are the new flagship pricing
        # tier ($10/$50). Before v1.6.7 they fell through to sonnet ($3/$15),
        # undercounting cost ~3.3x — the entire point of this regression guard.
        for m in (
            "claude-fable-5",
            "claude-mythos-5",
            "claude-fable-5-20260609",
            "CLAUDE-FABLE-5",
            "claude-fable-6-future",
        ):
            self.assertEqual(self.mod.tier(m), "fable", f"{m} → fable")

    def test_opus_old_covers_legacy_4_0_and_4_1(self):
        # Original Opus 4 / 4.1 billed at the older, higher rate.
        for m in (
            "claude-opus-4-0",
            "claude-opus-4-1",
            "claude-opus-4-1-20240801",
        ):
            self.assertEqual(self.mod.tier(m), "opus_old", f"{m} → opus_old")

    def test_haiku_covers_all_haiku_variants(self):
        for m in ("claude-haiku-4-5-20251001", "claude-haiku-5", "haiku-demo"):
            self.assertEqual(self.mod.tier(m), "haiku", f"{m} → haiku")

    def test_sonnet_is_default(self):
        # Both explicit Sonnet names and unknown families default to sonnet
        # (safe fallback — undercharges rather than overcharges for new models).
        for m in ("claude-sonnet-4-6", "claude-sonnet-5", "mystery-model-v2", ""):
            self.assertEqual(self.mod.tier(m), "sonnet", f"{m} → sonnet")

    def test_case_insensitive(self):
        # tier() lowercases input before matching — version suffixes with
        # mixed case (seen on some proprietary forks) still classify correctly.
        self.assertEqual(self.mod.tier("CLAUDE-OPUS-4-7"), "opus_new")
        self.assertEqual(self.mod.tier("Claude-Haiku-5"), "haiku")

    def test_pricing_table_has_all_tiers(self):
        # Every key tier() can return must be in PRICING or scan() falls
        # through to sonnet pricing — masking the bug.
        for t in ("fable", "opus_new", "opus_old", "sonnet", "haiku"):
            self.assertIn(t, self.mod.PRICING)
            p = self.mod.PRICING[t]
            for k in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
                self.assertIn(k, p, f"{t} missing {k}")

    def test_fable_pricing_matches_published_rates(self):
        # https://platform.claude.com/docs/en/about-claude/pricing (2026-06-09)
        p = self.mod.PRICING["fable"]
        self.assertEqual((p["input"], p["output"], p["cache_write_5m"],
                          p["cache_write_1h"], p["cache_read"]),
                         (10, 50, 12.5, 20, 1.00))


class TierCostEndToEndTest(unittest.TestCase):
    """Verify scan()'s cost math actually uses the classified tier."""

    def setUp(self):
        self.mod = load_plugin()
        self.tmp = tempfile.mkdtemp(prefix="cc-tier-test-")
        os.makedirs(os.path.join(self.tmp, "projects", "p"))
        self.jsonl = os.path.join(self.tmp, "projects", "p", "s.jsonl")
        self.mod.CLAUDE_DIR = self.tmp
        self.mod.SCAN_CACHE_FILE = Path(self.tmp) / ".scan-cache.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, mid, model, inp, out):
        return json.dumps({
            "type": "assistant",
            "timestamp": "2026-04-15T10:00:00Z",
            "message": {"id": mid, "model": model,
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}},
        })

    def test_opus_new_vs_opus_old_differ_3x(self):
        with open(self.jsonl, "w") as f:
            f.write(self._row("a", "claude-opus-4-7", 1_000_000, 0) + "\n")
            f.write(self._row("b", "claude-opus-4-1", 1_000_000, 0) + "\n")
        s = self.mod.scan()
        # opus_new input = $5/M, opus_old = $15/M → ratio 3.0 exactly
        costs = {m: v["cost"] for m, v in s["models"].items()}
        new_cost = costs["claude-opus-4-7"]
        old_cost = costs["claude-opus-4-1"]
        self.assertAlmostEqual(old_cost / new_cost, 3.0, places=4,
                               msg=f"opus_old should be 3x opus_new, got {old_cost/new_cost:.4f}")

    def test_cache_write_splits_by_ttl(self):
        # New JSONL format includes cache_creation.{5m,1h} breakdown.
        # Sonnet: 5m $3.75/M, 1h $6/M — ratio 1.6. If plugin forgets the
        # split and treats everything as 1h, the 5m batch overcharges by 60%.
        row = {
            "type": "assistant",
            "timestamp": "2026-04-15T10:00:00Z",
            "message": {
                "id": "x", "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 2_000_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 1_000_000,
                        "ephemeral_1h_input_tokens": 1_000_000,
                    },
                },
            },
        }
        with open(self.jsonl, "w") as f:
            f.write(json.dumps(row) + "\n")
        s = self.mod.scan()
        # 1M @ $3.75 + 1M @ $6 = $9.75
        self.assertAlmostEqual(s["cost"], 9.75, places=4)

    def test_fable_is_2x_opus_and_priced_absolutely(self):
        # Fable input $10/output $50 vs Opus 4.8 $5/$25 → exactly 2x. Guards
        # against a regression that re-prices Fable as sonnet (0.6x) or opus (0.5x).
        with open(self.jsonl, "w") as f:
            f.write(self._row("a", "claude-fable-5", 1_000_000, 1_000_000) + "\n")
            f.write(self._row("b", "claude-opus-4-8", 1_000_000, 1_000_000) + "\n")
        s = self.mod.scan()
        costs = {m: v["cost"] for m, v in s["models"].items()}
        self.assertAlmostEqual(costs["claude-fable-5"] / costs["claude-opus-4-8"],
                               2.0, places=4)
        # Absolute: 1M input @ $10 + 1M output @ $50 = $60
        self.assertAlmostEqual(costs["claude-fable-5"], 60.0, places=4)


class ModelShortNameTest(unittest.TestCase):
    """model_short() display-name derivation, incl. the Fable/Mythos scheme."""

    def setUp(self):
        self.mod = load_plugin()

    def test_opus_major_minor(self):
        self.assertEqual(self.mod.model_short("claude-opus-4-8"), "Opus 4.8")
        self.assertEqual(self.mod.model_short("claude-opus-4-8[1m]"), "Opus 4.8")
        self.assertEqual(self.mod.model_short("claude-opus-4-5-20250918"), "Opus 4.5")

    def test_fable_mythos_single_number(self):
        # Single-number versioning. A trailing date stamp must NOT be misread
        # as a minor version (the bug would yield 'Fable 5.20260609').
        self.assertEqual(self.mod.model_short("claude-fable-5"), "Fable 5")
        self.assertEqual(self.mod.model_short("claude-mythos-5"), "Mythos 5")
        self.assertEqual(self.mod.model_short("claude-fable-5-20260609"), "Fable 5")
        self.assertEqual(self.mod.model_short("claude-fable-6"), "Fable 6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
