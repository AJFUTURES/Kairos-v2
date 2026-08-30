# Changelog

## 2026-08-30 — Comprehensive private-review release

- Replaced the reference Pine indicator with the complete sanitized v7 signal and visual suite.
- Moved every entry alert from candle 2 to strict candle-3 close confirmation.
- Added session-liquidity A+ classification: Asia/London/New York H/L sweep followed by the qualifying IFVG.
- Added complete execution metadata: candle-1 protection, swing stop, entry reference, imbalance, sweep extreme, A+ flag/target, and structural exits.
- Added A+ bracket handling: candle-1 low/high protection during order creation, then all-stop-confirmed transition to swing H/L after the candle-3-confirmed fill.
- Kept the maximum swing-stop safety cap active for A+ runners and retained candle-1 protection whenever a broker stop modification is not fully confirmed.
- Updated the dashboard's A+ wording, expanded the README into a complete feature guide, retained every existing screenshot, and added static release-contract tests.
- Kept credentials, runtime state, logs, results, and private business files out of the repository.
