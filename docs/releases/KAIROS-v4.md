# KAIROS v4 / IFVG Pro v8

Release date: 2026-09-21

## What changed

- IFVG Pro v8 now includes native, visual-only NWOG, NDOG ETH, and NDOG RTH
  layers with independent drawing controls, history, extension modes, Event
  Horizon, and configurable RTH reference times.
- Normal structural trades keep the candle-1 broker stop attached in phase 2.
  The bot still watches the matching entry timeframe for a confirmed close beyond
  the same level and requests a direction-gated market exit.
- Entry and flatten requests require both HTTP 200 and broker `success: true`.
- A failed or unconfirmed flatten leaves protective orders and recovery tracking
  in place. Cleanup happens only after the broker confirms the position is flat.
- A rejected break-even stop is left eligible for retry.

## What did not change

- Strict candle-3 IFVG confirmation.
- The overlap and liquidity-sweep requirements.
- A+ qualification, swing transition, and target selection.
- Sizing, no-stacking, anti-hedging, filters, targets, and webhook fields.
- The Trade Centre and Command Centre feature set.

## Required update steps

1. Back up the existing local `.env` and `bot_state.json` without sharing them.
2. Replace the application files with KAIROS v4, then restore only those local
   runtime files.
3. Run `./start.sh --check`.
4. Replace the Pine source with IFVG Pro v8 and add it to the chart.
5. Delete and recreate every TradingView alert using **IFVG Pro v8 → Any alert()
   function call** and **Once Per Bar Close**.
6. Validate the full path using a Practice/simulated account before considering
   any non-practice account.

## Licensing note

The NWOG/NDOG module in `alertbot.pine` is adapted from **ICT NWOG/NDOG
(fadi)** by **fadizeidan** under MPL 2.0. The source preserves the notice,
attribution, and SPDX identifier; see `LICENSES/MPL-2.0-NOTICE.md`.

## Risk notice

This software can transmit futures orders. It is provided as-is, is not financial
advice, and does not guarantee performance. Users are responsible for their own
settings, connectivity, broker permissions, and losses.
