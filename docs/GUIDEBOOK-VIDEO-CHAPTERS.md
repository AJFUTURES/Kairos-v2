# KAIROS v4 / IFVG Pro v8 guidebook and video chapters

This structure is designed to serve both a readable PDF and a chaptered video.
The PDF can expand each chapter with screenshots and examples; the video can use
the same order and on-screen demonstrations.

## Chapter 1 — What KAIROS and IFVG Pro are

- Separate the indicator's signal/visual role from the bot's execution role.
- Show the path: chart → strict candle-3 alert → authenticated webhook → filters
  and risk rules → broker bracket → Command Centre and Trade Centre.
- State clearly that the tool combines multiple forms of chart context but that
  each module has a defined purpose and the open-gap layer is visual-only.
- Include the futures-risk, connectivity, and no-performance-guarantee notice.

## Chapter 2 — The strict IFVG setup

- Define the original FVG, opposite FVG, overlap, liquidity sweep, and candle 3.
- Explain why signals wait for the candle-3 close and do not fire during candle 2.
- Walk through bullish and bearish examples, including a rejected setup.
- Explain minimum imbalance and lookback inputs without implying profitability.

## Chapter 3 — Sessions, liquidity, and A+ classification

- Configure Asia, London, and New York sessions and timezone behavior.
- Explain session highs/lows, the allowed sweep-to-IFVG sequence, and the lookback.
- Define A+ as a mechanical classification, not a promise of quality or returns.
- Show A+ initial candle-1 protection, swing transition, session cap, HTF target,
  and fallback target.

## Chapter 4 — Chart drawings and display controls

- IFVG boxes, labels, consequent encroachment, projection, and invalidation style.
- Pivot-sweep markers and sensitivity.
- Color themes, label sizes, delete/dim behavior, and decluttering presets.
- Explain which drawings are analytical context and which levels enter the payload.

## Chapter 5 — Higher-timeframe FVGs and bias

- Configure 15-minute, 1-hour, and 4-hour overlays.
- Explain filled versus unfilled visibility and last-1/3/5 limits.
- Explain the 5m/15m/1h/4h/12h bias table and respected-FVG logic.
- Make clear that HTF bias is context and does not silently rewrite the strict
  entry condition.

## Chapter 6 — NWOG, NDOG ETH, and NDOG RTH

- Define each gap family and its chart purpose.
- Demonstrate current/historical boundaries, CE, backgrounds, labels, history,
  nearest/proximity/always extension, and the NWOG Event Horizon.
- Explain the configurable RTH timezone and close/open reference times, including
  the default New York 16:14 / 18:00 values.
- State and visually prove that this module does not alter signals, alerts, A+
  classification, stops, targets, or webhook JSON.
- Credit **ICT NWOG/NDOG (fadi)** by **fadizeidan** and show the MPL 2.0 notice.

## Chapter 7 — Alert modes and secure webhook setup

- Text, Webhook JSON, and Both modes.
- Add the secret through Settings; never paste broker credentials into Pine.
- Explain every execution field: symbol, action, timeframe, candle-1 level,
  swing level, entry reference, imbalance, sweep extreme, A+ flag, and target.
- Create a fresh IFVG Pro v8 alert using Any `alert()` function call and Once Per
  Bar Close. Explain why Pine updates require old alerts to be recreated.

## Chapter 8 — Risk settings overview

- Global pause, enabled instruments, direction controls, and account-size presets.
- Structural versus Swing stop; fixed versus multiple-based take-profit.
- Minimum FVG and maximum swing-stop filters by market group.
- Macro window, 1-minute session restriction, no-stacking, anti-hedging, A+ cap,
  consecutive-loss pause, and adaptive daily-loss stop.
- Include a pre-session checklist and a one-variable-at-a-time settings worksheet.

## Chapter 9 — Structural stop loss: exact behavior

- Long level = candle 1 low; short level = candle 1 high.
- The entry is sent with a broker-side bracket, then the stop is pinned to the
  exact level after the fill is visible.
- Phase 1 covers order creation and the entry candle with hard broker protection.
- In phase 2, KAIROS v4 keeps that broker stop attached continuously and also
  evaluates confirmed closes on the entry timeframe at the same level.
- The close monitor is direction- and timeframe-gated so stale exit alerts cannot
  close a flipped or unrelated position.
- If the structural level is missing or unusable, document the configured safety
  fallback. Do not describe it as equivalent to a valid candle-1 level.
- Show four diagrams: long normal, short normal, intrabar stop execution, and a
  confirmed-close exit request. Explicitly explain that continuous broker
  protection means wick tolerance is not guaranteed.

## Chapter 10 — Swing stop and A+ stop behavior

- Swing mode uses the completed-bar swing low/high as a persistent broker stop.
- Explain the maximum-distance rejection before entry.
- For A+, show candle-1 protection during order creation and the all-stops-confirmed
  move to the supplied swing level after the candle-3-confirmed fill.
- If that modification is not fully confirmed, candle-1 protection remains and
  the bot retries rather than pretending the transition succeeded.

## Chapter 11 — Auto-break-even: exact behavior

- Auto-BE is optional and is based on the live position and live take-profit.
- Trigger distance = 50% of the distance from average entry to the live TP.
- Long BE stop = average entry plus one tick; short BE stop = average entry minus
  one tick. This aims to cover one tick, not commissions, slippage, or every fee.
- The bot discovers live stop orders before modifying them and recovers missing IDs.
- If price has already returned through the intended BE price, KAIROS requests a
  guarded flatten rather than sending an invalid stop.
- If the broker rejects the replacement BE stop, v4 records no false success and
  leaves the monitor eligible to retry.
- Every live stop must be modified successfully before BE is reported as armed;
  a partial modification is logged and retried.
- If a flatten is not confirmed, existing protection and recovery tracking remain.
- Show examples for normal structural, Swing, A+, stacked legacy state, thin quote
  data, and a broker rejection. Note that adaptive mode uses fixed geometry and
  skips the ordinary auto-BE path.

## Chapter 12 — Sizing, targets, and adaptive mode

- Custom/50K/100K/150K quantities and the one-contract full-size cap.
- Structural 2x/3x/5x/7x targets and flat point targets by market group.
- A+ HTF FVG edge and fallback target.
- Adaptive micros-only multiplier, win growth, consecutive-loss cut, floor,
  break-even band, fixed geometry, one-position lock, and manual daily-stop resume.
- Separate account labels from risk capacity; no preset guarantees suitability.

## Chapter 13 — Command Centre, Trade Centre, and notifications

- Verify account, positions, broker streams, settings, feed, and pause state.
- Use Test Buy/Test Sell as dry runs; explain what they do not prove.
- Review normalized history, filters, metrics, equity/drawdown, heatmap, ledger,
  notes/tags, CSV, and JSON.
- Configure Discord/Telegram notifications without exposing secrets.

## Chapter 14 — Installation and Practice acceptance

- Download the verified public release, create `.env`, run the safe preflight,
  configure the tunnel, and launch one instance only.
- Compile IFVG Pro v8 in TradingView and recreate alerts.
- Perform the Practice checklist: authenticated signal, candle-3 timing, correct
  payload, attached stop/TP, structural phase-2 protection, auto-BE, rejection
  handling, restart recovery, and no orphan orders.
- Never use an evaluation, funded, or live account for initial acceptance.

## Chapter 15 — Troubleshooting and update discipline

- Trace ignored signals from the activity-feed reason.
- Diagnose TradingView webhook delivery, secret mismatch, symbol mapping, broker
  rejection, missing quotes, and tunnel failures.
- Preserve `.env` and state locally, replace application files, run preflight, and
  recreate alerts after every Pine update.
- Verify release checksums and use only the official GitHub/Discord artifacts.

## Suggested supporting visuals

- System-flow diagram and payload field map.
- Bullish/bearish strict IFVG anatomy.
- A+ sequence timeline.
- Structural stop phase diagram with broker-order state at every step.
- Auto-BE long/short price ladders with trigger, entry, one-tick BE, and TP.
- Comparison table: Structural vs Swing vs A+ vs Adaptive.
- NWOG/NDOG annotated chart and settings screenshots.
- Practice acceptance checklist as the final printable page.
