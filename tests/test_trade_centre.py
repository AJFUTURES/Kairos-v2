import json
import tempfile
import unittest
from pathlib import Path

from trade_centre import (
    build_trade_centre_payload,
    load_reviews,
    merge_history,
    normalize_working_rows,
    parse_results_text,
    save_review,
)


RESULTS = """
========================================================================
SESSION 2026-09-02  (Wed 02 Sep 2026 · 6:00 PM NY open)
========================================================================
[entry 10:04 AM NY -> exit 10:46 AM NY] NQ   BUY  x1  A+ structural -> TP hit                    +$665.80  (SL 16.0pt / TP 33.5pt @ exit 23451.75 · 5m Bullish IFVG [A+])
[entry 03:18 AM NY -> exit 03:42 AM NY] ES   SELL x2  Swing     -> Stop · swing              -$240.00  (SL 2.25pt / TP 5.0pt · 1m Bearish IFVG)
"""


class TradeCentreTests(unittest.TestCase):
    def test_parse_results_and_derive_review_fields(self):
        raw, warnings = parse_results_text(RESULTS)
        self.assertEqual(warnings, [])
        trades = merge_history(raw, [])
        self.assertEqual(len(trades), 2)
        nq = next(row for row in trades if row["instrument"] == "NQ")
        self.assertTrue(nq["a_plus"])
        self.assertEqual(nq["session_name"], "New York")
        self.assertEqual(nq["planned_risk_usd"], 320.0)
        self.assertAlmostEqual(nq["realized_r"], 2.081, places=3)
        self.assertEqual(nq["entry_at_utc"], "2026-09-03T14:04:00Z")

    def test_working_row_merges_without_duplicate(self):
        raw, _ = parse_results_text(RESULTS)
        working = normalize_working_rows([{
            "session": "2026-09-02", "time_ny": "10:04 AM", "exit_ny": "10:46 AM",
            "instrument": "NQ", "direction": "BUY", "size": "1",
            "stop_mode": "A+ structural", "a_plus": True, "sl_pts": 16.0,
            "tp_pts": 33.5, "pnl": "+$665.80", "outcome": "TP hit",
            "ifvg_source": "5m Bullish IFVG", "be_fired": True,
        }])
        trades = merge_history(raw, working)
        self.assertEqual(len(trades), 2)
        nq = next(row for row in trades if row["instrument"] == "NQ")
        self.assertTrue(nq["be_fired"])
        self.assertEqual(set(nq["data_sources"]), {"results.txt", "bot_state trade_log"})

    def test_reviews_are_separate_sanitized_and_reloadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trade_reviews.json"
            review = save_review(path, "ktr_example123", " Followed the plan. ", ["A+", "<script>", "a+"])
            self.assertEqual(review["notes"], "Followed the plan.")
            self.assertEqual(review["tags"], ["A+", "script"])
            stored = load_reviews(path)
            self.assertEqual(stored["ktr_example123"]["notes"], "Followed the plan.")
            parsed = json.loads(path.read_text())
            self.assertEqual(parsed["schema_version"], 1)

    def test_payload_handles_missing_and_malformed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "results.txt"
            results.write_text("SESSION 2026-09-02 (test)\n[entry malformed\n")
            payload = build_trade_centre_payload(results, [], root / "reviews.json")
            self.assertEqual(payload["trades"], [])
            self.assertTrue(payload["meta"]["parse_warnings"])


if __name__ == "__main__":
    unittest.main()
