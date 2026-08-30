"""Behavior checks for the side-effect-free adaptive sizing engine."""

import unittest

import adaptive_sizing as adaptive


class AdaptiveSizingTests(unittest.TestCase):
    def setUp(self):
        self.settings = adaptive.default_settings()
        self.state = adaptive.default_state()

    def test_win_grows_multiplier_and_contract_count(self):
        self.settings["win_growth"] = 2.0
        event = adaptive.apply_close(self.state, self.settings, 100.0, "2026-08-30")
        self.assertTrue(event["grew"])
        self.assertEqual(2.0, self.state["mult"])
        self.assertEqual(2, adaptive.contracts_for(self.state["mult"], 1))

    def test_configured_loss_run_cuts_at_floor(self):
        self.settings.update(cut_after=2, loss_cut=0.5, floor_mult=0.75)
        adaptive.apply_close(self.state, self.settings, -100.0, "2026-08-30")
        event = adaptive.apply_close(self.state, self.settings, -100.0, "2026-08-30")
        self.assertTrue(event["cut"])
        self.assertEqual(0.75, self.state["mult"])

    def test_break_even_is_neutral(self):
        before = self.state.copy()
        event = adaptive.apply_close(self.state, self.settings, 10.0, "2026-08-30")
        self.assertEqual("neutral", event["kind"])
        self.assertEqual(before["mult"], self.state["mult"])
        self.assertEqual(before["loss_run"], self.state["loss_run"])
        self.assertEqual(before["daily_losses"], self.state["daily_losses"])

    def test_daily_stop_requires_manual_resume(self):
        self.settings.update(daily_loss_limit=1, cut_after=99)
        event = adaptive.apply_close(self.state, self.settings, -100.0, "2026-08-30")
        self.assertTrue(event["stop_tripped"])
        adaptive.apply_close(self.state, self.settings, 100.0, "2026-08-31")
        self.assertTrue(self.state["stopped"])
        self.assertEqual(0, self.state["daily_losses"])


if __name__ == "__main__":
    unittest.main()
