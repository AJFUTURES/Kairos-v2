# KAIROS v4 / IFVG Pro v8 release plan

## Approved scope

- Keep broker-side protection attached during structural phase 2.
- Treat broker HTTP responses as successful only when the response body also
  reports `success: true`.
- Preserve protective orders and local tracking until a flatten is confirmed.
- Merge the visual-only NWOG, NDOG ETH, and NDOG RTH chart layer into IFVG Pro v8.
- Preserve strict candle-3 signals, A+ qualification, sizing, target rules,
  webhook fields, and all unrelated trading behavior.
- Publish a sanitized KAIROS v4 package and standalone IFVG Pro v8 source.
- Update customer documentation, licensing, and release notes; keep the
  guidebook/video chapter plan as an owner-only production aid.
- Validate locally, compile in TradingView, and use no evaluation, funded, or live
  account for validation.

## Release gates

- [x] Execution-safety regression tests pass.
- [x] Full local release suite passes.
- [x] Pine source compiles in TradingView.
- [x] NWOG/NDOG source, settings, and chart-load parity is reviewed.
- [x] Sanitized package and checksums are verified.
- [x] GitHub release is published.
- [x] Discord release post and attachments are verified.

## Progress

- 2026-09-21: Created a clean worktree from public `origin/main` so the owner's
  separate unfinished checkout remains untouched.
- 2026-09-21: Scope A1-A7 and the revised public announcement language were
  approved by the owner.
- 2026-09-21: Implementation started.
- 2026-09-21: Added continuous structural protection, broker business-status
  validation, confirmed-flat cleanup, retry-safe break-even handling, and eight
  broker-free execution-safety scenarios.
- 2026-09-21: Ported the visual-only open-gap module as IFVG Pro v8, preserved
  MPL 2.0 attribution, and updated the public guides and release notes.
- 2026-09-21: All 29 automated tests, shell syntax checks, Python compilation,
  and whitespace checks pass.
- 2026-09-21: Loaded the exact committed 1,305-line source in TradingView. It
  compiled without a Pine error, appeared on the chart as `IFVG Pro v8`, and
  exposed the full NWOG/NDOG settings surface. Review covered source, controls,
  and successful chart loading; it was not a pixel-by-pixel historical replay.
- 2026-09-21: Built the sanitized `KAIROS-v4.zip`, standalone
  `IFVG-Pro-v8.pine`, release notes, and SHA-256 manifest. The ZIP integrity
  check passes and contains no private runtime files or owner-only production
  notes.
- 2026-09-22: Published the GitHub release and Discord `@everyone`
  announcement. Discord message `1551783860738592829` is pinned.
- 2026-09-22: Removed the owner-only guidebook/video chapter outline from the
  Discord message, GitHub release assets, tagged source, public ZIP, and public
  checksum manifest. Replaced the public ZIP and checksum assets with clean
  versions; the private outline remains only in the owner's local distribution
  folder.

## Stop condition

Stop after the GitHub and Discord release artifacts are verified, the private
guidebook chapter plan is delivered directly to the owner, and all remaining
manual limitations are recorded.
