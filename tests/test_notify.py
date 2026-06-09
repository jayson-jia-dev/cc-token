#!/usr/bin/env python3
"""Regression tests for check_and_notify() dedup / escalation logic.

v1.4.5 rewrote notification throttling after real-world spam: a single
0→100% jump used to fire THREE notifications (80 + 95 + 100 tiers all
crossed simultaneously), and every 5h-window rollover re-fired the
whole set. Users reported 4× pushes at 18:52 and again at 19:02 across
the boundary.

New semantics locked down here:
- Escalation fires once per tier crossing.
- Jumping multiple tiers in one step is a single notification.
- Same-tier repeat does not re-fire.
- De-escalation to 0 clears state so a re-crossing fires again.
- Reset rollover (different resets_at with same utilization) does NOT
  re-fire (tier-key has no reset stamp).
- Burn-rate is suppressed once five_hour is already at 100%.

Run with:  python3 tests/test_notify.py
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


class CheckAndNotifyTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.tmp = tempfile.mkdtemp(prefix="cc-notify-test-")
        self.mod.NOTIFY_STATE_FILE = Path(self.tmp) / ".notify-state.json"
        self.mod.CFG = dict(self.mod.CFG)
        self.mod.CFG["notifications"] = True

        # Capture _notify calls instead of firing real osascript.
        self.fired = []
        self._orig_notify = self.mod._notify
        self.mod._notify = lambda title, msg: self.fired.append((title, msg))

    def tearDown(self):
        self.mod._notify = self._orig_notify
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _usage(self, five_hour_util=None, seven_day_util=None, five_hour_reset=None):
        # A near-now reset_at suppresses burn-rate: elapsed≈300min, so rate
        # stays low and min_to_full exceeds the 30min alert threshold for any
        # tier-80 utilization. Tests that specifically care about rollover
        # supply their own reset stamps.
        from datetime import datetime as _dt, timezone
        if five_hour_reset is None:
            five_hour_reset = _dt.now(timezone.utc).isoformat().replace("+00:00", "Z")
        u = {}
        if five_hour_util is not None:
            u["five_hour"] = {"utilization": five_hour_util, "resets_at": five_hour_reset}
        if seven_day_util is not None:
            u["seven_day"] = {"utilization": seven_day_util, "resets_at": "2026-04-28T00:00:00Z"}
        return u

    def _state(self):
        if self.mod.NOTIFY_STATE_FILE.is_file():
            return json.loads(self.mod.NOTIFY_STATE_FILE.read_text())
        return {}

    def _tier_fires(self):
        # Burn-rate notifications are a separate axis — they can fire
        # alongside tier crossings depending on utilization-vs-reset math
        # and aren't the subject of the dedup guarantees tested here.
        # Filter them out so these tests verify tier-crossing behavior
        # in isolation.
        return [f for f in self.fired if "🔥" not in f[0]]

    # ── Escalation ────────────────────────────────────────────────
    def test_zero_to_100_fires_single_notification(self):
        # THE regression this rewrite was for: jumping multiple tiers in one
        # check used to fire 80 + 95 + 100 all at once. At 100% burn is
        # suppressed (tier<100 gate), so self.fired should contain exactly
        # one notification — the tier 100 one.
        self.mod.check_and_notify(self._usage(five_hour_util=100))
        self.assertEqual(len(self.fired), 1, f"expected 1, got {len(self.fired)}: {self.fired}")
        title = self.fired[0][0]
        self.assertIn("100", title)

    def test_single_tier_crossing_fires_once(self):
        self.mod.check_and_notify(self._usage(five_hour_util=82))
        self.assertEqual(len(self._tier_fires()), 1)
        self.assertIn("82", self._tier_fires()[0][0])

    def test_same_tier_repeat_does_not_refire(self):
        self.mod.check_and_notify(self._usage(five_hour_util=85))
        self.assertEqual(len(self._tier_fires()), 1)
        self.fired.clear()
        # util went up slightly but still in tier 80 → no new notification
        self.mod.check_and_notify(self._usage(five_hour_util=88))
        self.assertEqual(len(self._tier_fires()), 0)

    def test_escalation_80_to_95_fires_once(self):
        self.mod.check_and_notify(self._usage(five_hour_util=82))
        self.assertEqual(len(self._tier_fires()), 1)
        self.fired.clear()
        self.mod.check_and_notify(self._usage(five_hour_util=96))
        tf = self._tier_fires()
        self.assertEqual(len(tf), 1)
        self.assertIn("96", tf[0][0])

    def test_de_escalation_clears_state(self):
        # Cross 80 → util falls to 40 → clear state → re-cross 80 fires again.
        self.mod.check_and_notify(self._usage(five_hour_util=82))
        self.mod.check_and_notify(self._usage(five_hour_util=40))  # drop → clear
        self.fired.clear()
        self.mod.check_and_notify(self._usage(five_hour_util=85))
        self.assertEqual(len(self._tier_fires()), 1,
                         "re-crossing after drop below 80 must re-fire")

    def test_window_rollover_does_not_refire(self):
        # Cross 95 with one reset timestamp, then same 95 with a new reset
        # stamp (window rolled over). Tier state has no reset-stamp, so
        # tier notifications must NOT re-fire across the boundary.
        self.mod.check_and_notify(self._usage(five_hour_util=96,
                                              five_hour_reset="2026-04-21T15:00:00Z"))
        self.assertEqual(len(self._tier_fires()), 1)
        self.fired.clear()
        self.mod.check_and_notify(self._usage(five_hour_util=96,
                                              five_hour_reset="2026-04-21T20:00:00Z"))
        self.assertEqual(len(self._tier_fires()), 0,
                         "window rollover must not re-fire same tier")

    def test_below_80_fires_nothing(self):
        self.mod.check_and_notify(self._usage(five_hour_util=79))
        self.assertEqual(self._tier_fires(), [])

    def test_notifications_disabled_fires_nothing(self):
        self.mod.CFG["notifications"] = False
        self.mod.check_and_notify(self._usage(five_hour_util=100))
        self.assertEqual(self.fired, [])

    # ── Multiple limits in same check ─────────────────────────────
    def test_different_limits_fire_independently(self):
        # Session 95 AND Weekly 82 → two distinct tier notifications, one
        # per limit (burn may also fire for five_hour but is filtered out).
        self.mod.check_and_notify(self._usage(five_hour_util=96, seven_day_util=82))
        tf = self._tier_fires()
        self.assertEqual(len(tf), 2)
        titles = [f[0] for f in tf]
        self.assertTrue(any("Session" in t for t in titles))
        self.assertTrue(any("Weekly" in t for t in titles))

    # ── State persistence ─────────────────────────────────────────
    def test_state_file_written_with_tier(self):
        self.mod.check_and_notify(self._usage(five_hour_util=85))
        s = self._state()
        self.assertIn("five_hour", s)
        self.assertEqual(s["five_hour"]["tier"], 80)

    def test_state_file_legacy_format_dropped(self):
        # Pre-v1.4.5 files had string values — load must drop them silently
        # rather than crash or persist them forever.
        self.mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.mod.NOTIFY_STATE_FILE.write_text(json.dumps({
            "five_hour_80_2026-04-21T15:00:00Z": "fired",  # legacy
        }))
        # Should not raise. Should fire fresh notification (legacy entry ignored).
        self.mod.check_and_notify(self._usage(five_hour_util=82))
        self.assertEqual(len(self._tier_fires()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
