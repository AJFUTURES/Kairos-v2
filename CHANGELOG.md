# Changelog

## 2026-09-03 — Trade Centre

- Added a separate authenticated Trade Centre with shared filters, performance
  metrics, native charts, a time heatmap, a sortable ledger, and readable
  per-trade review details.
- Normalized legacy `results.txt` and richer recent `trade_log` rows without
  changing trading behavior or the existing Command Centre.
- Added private notes/tags plus documented CSV and JSON exports.
- See the [minimal release note](docs/releases/trade-centre-2026-09-03.md).

## 2026-08-31 — Customer self-service and one-click readiness

- Added `HOW-TO-USE.md`, a complete private-access, credentials, Cloudflare, TradingView, Practice-test, one-click launch, daily-use and troubleshooting walkthrough.
- Added customer-facing `AGENTS.md` with architecture, repository navigation, code entry points, safety rules, verification and AI-assistant prompts.
- Documented the personal macOS `KAIROS.command` launcher in both guides while deliberately excluding and ignoring `*.command` files.
- Made `start.sh` bootstrap the virtual environment and pinned dependencies, validate keys without printing them, support a no-broker `--check`, start a configured tunnel, and handle paths containing spaces.
- Made both launch scripts executable and removed broad process/port kills; KAIROS now stops only its own path-verified PID and refuses to terminate an unknown application.
- Clarified that `PROJECT_X_ACCOUNT_ID` expects the exact active account name, added optional `KAIROS_PUBLIC_URL`, and expanded release tests for the customer-launch contract.
- Added no-store, no-referrer, anti-frame and MIME-sniffing protection headers to dashboard/login responses.
- Aligned sizing summaries and dashboard limits with actual execution: same-direction signals never stack, so displayed maximum size now equals the single fresh-position quantity.

## 2026-08-30 — Comprehensive private-review release

- Replaced the reference Pine indicator with the complete sanitized v7 signal and visual suite.
- Moved every entry alert from candle 2 to strict candle-3 close confirmation.
- Added session-liquidity A+ classification: Asia/London/New York H/L sweep followed by the qualifying IFVG.
- Added complete execution metadata: candle-1 protection, swing stop, entry reference, imbalance, sweep extreme, A+ flag/target, and structural exits.
- Added A+ bracket handling: candle-1 low/high protection during order creation, then all-stop-confirmed transition to swing H/L after the candle-3-confirmed fill.
- Kept the maximum swing-stop safety cap active for A+ runners and retained candle-1 protection whenever a broker stop modification is not fully confirmed.
- Updated the dashboard's A+ wording, expanded the README into a complete feature guide, retained every existing screenshot, and added static release-contract tests.
- Kept credentials, runtime state, logs, results, and private business files out of the repository.
