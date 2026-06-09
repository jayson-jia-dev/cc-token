"""Unit test for _update_failure_streak + _maybe_warn_update_stuck."""
import sys, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load plugin module
import importlib.util
spec = importlib.util.spec_from_file_location("cc", str(ROOT / "cc-token.5m.py"))
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

class StreakTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "update.log"
        self.notified = Path(self.tmp.name) / "notified"
        cc.UPDATE_LOG_FILE = self.log
        cc.UPDATE_NOTIFIED_FILE = self.notified
    def tearDown(self):
        self.tmp.cleanup()

    def test_no_log_returns_zero(self):
        self.assertEqual(cc._update_failure_streak(), 0)

    def test_empty_log_returns_zero(self):
        self.log.write_text("")
        self.assertEqual(cc._update_failure_streak(), 0)

    def test_last_line_success_returns_zero(self):
        self.log.write_text(
            "2026-04-20T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-21T10:00:00 v1.5.0 updated 1.5.0 → 1.5.1\n"
        )
        self.assertEqual(cc._update_failure_streak(), 0)

    def test_three_failures_then_success_stops_at_zero(self):
        # Success is older but more recent than... wait — we walk BACKWARDS
        # so most recent (bottom) wins. Here 3 failures AT THE TAIL with no
        # success after → streak 3.
        self.log.write_text(
            "2026-04-20T10:00:00 v1.5.0 updated 1.4.9 → 1.5.0\n"
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch for remote v1.5.2\n"
        )
        self.assertEqual(cc._update_failure_streak(), 3)

    def test_check_ok_counts_as_success(self):
        self.log.write_text(
            "2026-04-20T10:00:00 v1.5.0 error: something\n"
            "2026-04-21T10:00:00 v1.5.0 check OK: up-to-date (1.5.0)\n"
            "2026-04-22T10:00:00 v1.5.0 check failed: timeout\n"
        )
        # Walking back: first line is "check failed" → count=1. Next is "check OK" → stop, return 1.
        self.assertEqual(cc._update_failure_streak(), 1)

    def test_short_write_counts_as_failure(self):
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 short write: disk=100 expected=200\n"
            "2026-04-22T10:00:00 v1.5.0 error: foo\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
        )
        self.assertEqual(cc._update_failure_streak(), 3)

    def test_warn_returns_empty_when_healthy(self):
        self.log.write_text(
            "2026-04-22T10:00:00 v1.5.0 check OK: up-to-date (1.5.0)\n"
        )
        with patch.object(cc, '_notify') as mock_notify:
            self.assertEqual(cc._maybe_warn_update_stuck(), "")
            mock_notify.assert_not_called()

    def test_warn_returns_prefix_and_notifies_at_3(self):
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
        )
        with patch.object(cc, '_notify') as mock_notify:
            self.assertEqual(cc._maybe_warn_update_stuck(), "⚠️ ")
            mock_notify.assert_called_once()

    def test_warn_notifies_only_once_per_streak(self):
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
        )
        with patch.object(cc, '_notify') as mock_notify:
            cc._maybe_warn_update_stuck()  # notifies
            cc._maybe_warn_update_stuck()  # should NOT re-notify
            self.assertEqual(mock_notify.call_count, 1)

    def test_warn_re_notifies_when_streak_grows(self):
        # 3 failures → notified. Streak grows to 5 → should re-notify.
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
        )
        with patch.object(cc, '_notify') as mock_notify:
            cc._maybe_warn_update_stuck()
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
            "2026-04-24T10:00:00 v1.5.0 error: timeout\n"
            "2026-04-25T10:00:00 v1.5.0 error: timeout\n"
        )
        with patch.object(cc, '_notify') as mock_notify:
            cc._maybe_warn_update_stuck()
            mock_notify.assert_called_once()

    def test_warn_resets_on_recovery(self):
        # Stuck → notified marker written. Then success → marker cleared.
        self.log.write_text("2026-04-23T10:00:00 v1.5.0 error: a\n" * 1 +
                            "2026-04-23T10:00:00 v1.5.0 error: b\n" * 1 +
                            "2026-04-23T10:00:00 v1.5.0 error: c\n" * 1)
        self.log.write_text(
            "2026-04-21T10:00:00 v1.5.0 check failed: timeout\n"
            "2026-04-22T10:00:00 v1.5.0 error: URLError\n"
            "2026-04-23T10:00:00 v1.5.0 checksum mismatch\n"
        )
        with patch.object(cc, '_notify'):
            cc._maybe_warn_update_stuck()
        self.assertTrue(self.notified.exists())
        # Recovery
        self.log.write_text(
            "2026-04-24T10:00:00 v1.5.0 updated 1.5.0 → 1.5.1\n"
        )
        with patch.object(cc, '_notify'):
            self.assertEqual(cc._maybe_warn_update_stuck(), "")
        self.assertFalse(self.notified.exists())

if __name__ == "__main__":
    unittest.main(verbosity=2)
