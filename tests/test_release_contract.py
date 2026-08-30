"""Static release checks for the sanitized KAIROS distribution."""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_python_sources_parse(self):
        for relative in (
            "main.py",
            "adaptive_sizing.py",
            "Analytics/generate_analytics.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            ast.parse(source, filename=relative)

    def test_pine_uses_strict_candle_three_close(self):
        pine = (ROOT / "alertbot.pine").read_text(encoding="utf-8")
        self.assertIn("if barstate.isconfirmed and (isBullFVG or isBearFVG)", pine)
        self.assertIn("alert.freq_once_per_bar_close", pine)
        self.assertNotIn("YOUR_WEBHOOK_SECRET", pine)

    def test_pine_sends_complete_sanitized_execution_payload(self):
        pine = (ROOT / "alertbot.pine").read_text(encoding="utf-8")
        for field in (
            '"sl_price"',
            '"c1_high"',
            '"c1_low"',
            '"swing_sl"',
            '"entry_ref"',
            '"imbalance"',
            '"sweep_extreme"',
            '"a_plus"',
            '"a_plus_target"',
        ):
            self.assertIn(field, pine)

    def test_aplus_stop_transition_requires_all_live_stops(self):
        bot = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("_aplus_swing_transition", bot)
        self.assertGreaterEqual(bot.count("require_all_stops=True"), 2)
        self.assertIn("stop_total > 0 and stop_moved == stop_total", bot)
        self.assertIn('(trade_settings.get("swing_stop") or a_plus)', bot)

    def test_readme_keeps_notice_and_all_existing_images(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**IMPORTANT SIGNAL CHANGE:", readme)
        for relative in (
            "assets/dashboard_logs.png",
            "assets/instrument_filter.png",
            "assets/HTF_FVGs.png",
            "assets/results.png",
        ):
            self.assertIn(relative, readme)
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_runtime_secrets_and_state_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (".env", "bot_state.json", "*.log", "trade_logs.txt", "results.txt"):
            self.assertIn(pattern, ignore)


if __name__ == "__main__":
    unittest.main()
