# KAIROS — Code and AI Assistant Guide

Read this file before asking an AI coding assistant to explain, troubleshoot, or
change KAIROS. For the customer installation walkthrough, start with
[`HOW-TO-USE.md`](HOW-TO-USE.md); for the feature inventory, read
[`README.md`](README.md).

## What KAIROS does

KAIROS is a self-hosted futures execution system. TradingView's Pine indicator
detects a qualifying IFVG after candle 3 closes, sends an authenticated JSON
webhook, and the Python bot applies the customer's filters and risk rules before
placing a bracket order through ProjectX/TopstepX.

The most important signal contract is:

- Entry alerts are **strict candle-3-close only**; old candle-2 alerts must be recreated.
- A+ means an enabled Asia/London/New York session H/L sweep followed by the strict IFVG.
- A+ protection begins at candle 1's low for longs or high for shorts, then the
  filled bracket moves to the supplied swing low/high after candle-3 confirmation.
- Never run two KAIROS processes against the same account.

## How data moves

```text
TradingView alertbot.pine
        ↓ authenticated JSON over HTTPS
Cloudflare Tunnel → POST /webhook
        ↓ validation, filters, sizing, exposure and risk checks
main.py → ProjectX/TopstepX REST order API
        ↕ live positions, orders, fills, quotes and balance
SignalR websockets
        ↓
Protected dashboard + persistent local state
```

The tunnel only makes the local webhook/dashboard reachable over HTTPS. Broker
credentials remain in the customer's local `.env`; they are never placed in Pine,
GitHub, Cloudflare Pages, the README, or an AI prompt.

## Repository navigation

| Path | Purpose | Change it when… |
|---|---|---|
| `HOW-TO-USE.md` | Customer setup, tunnel, TradingView, one-click launch and daily use | The installation or launch flow changes |
| `README.md` | Product overview and complete feature guide | Features or user-visible behavior changes |
| `.env.example` | Blank configuration template | A new environment setting is added |
| `start.sh` | Safe foreground launcher and first-run dependency bootstrap | Startup/preflight behavior changes |
| `bot.sh` | Optional macOS background service controls | launchd/background behavior changes |
| `alertbot.pine` | TradingView signal engine, session/HTF visuals and JSON payload | Signal definitions, chart visuals or webhook fields change |
| `main.py` | Webhook, risk gates, execution, broker connections, state and dashboard API | Trading behavior or server behavior changes |
| `adaptive_sizing.py` | Pure micros-only sizing ladder | Adaptive multiplier/geometry rules change |
| `web/dashboard.html` | Live control panel | Dashboard controls or presentation change |
| `web/login.html` | Dashboard token entry page | Login presentation changes |
| `site/` | Optional static landing page | Public marketing page changes |
| `Analytics/` | Local results dashboard generator | Closed-trade analytics change |
| `deploy/` | Optional deployment references | Hosting/service instructions change |
| `tests/` | Automated product and release-contract checks | Any code, payload or release rule changes |
| `CHANGELOG.md` | Shipped behavior history | Every customer release |

Runtime files such as `.env`, `bot_state.json`, `.kairos.pid`, logs, results, the
virtual environment, tunnel credentials, and the customer's `KAIROS.command` are
local-only and ignored by Git.

## Finding the important code

### In `main.py`

- Startup configuration and required `.env` validation are at the top of the file.
- Instrument definitions, sizing presets, stop/target defaults, no-hedge groups,
  A+ defaults, cooldowns and adaptive defaults are in the constants/settings sections.
- `receive_webhook()` authenticates and parses TradingView payloads, then applies
  symbol, A+, time, imbalance, direction and maximum-stop filters.
- `execute_trade()` owns serialized exposure decisions: ignore, open, stack, or block.
- `place_order()` calculates quantities and brackets, implements structural/swing/A+
  protection, places the order, arms break-even, and records the trade.
- `structural_monitor_loop()`, `be_monitor_loop()` and orphan/hedge reconciliation
  maintain protection after entry.
- SignalR handlers reconcile broker positions, fills, orders, quotes and P&L.
- Routes beginning `/api/` provide the token-protected dashboard controls.
- The final `if __name__ == "__main__"` block starts the localhost-only server.

### In `alertbot.pine`

- **Inputs** define IFVG, visual, session, alerts, risk, HTF overlay and bias controls.
- **IFVG Core Logic** is the strict candle-3 + overlap + sweep gate and alert payload.
- **Session Liquidity** tracks Asia/London/New York highs, lows and matching A+ sweeps.
- **HTF FVG Overlay** tracks filled/unfilled 15m, 1h and 4h gaps.
- **HTF Bias** derives 5m, 15m, 1h, 4h and 12h bias from respected FVGs.

When changing a webhook field, update both `alertbot.pine` and `receive_webhook()`;
then update the payload example, README inventory, changelog and release tests.

## Normal customer usage

1. Complete every step in `HOW-TO-USE.md` using a practice account first.
2. Run `./start.sh --check` for a no-broker-connection preflight.
3. Launch with `./start.sh` or the customer's local `KAIROS.command` shortcut.
4. Open the dashboard using `DASHBOARD_TOKEN`, confirm the selected account,
   sizing, enabled instruments, direction, stop mode, filters and pause state.
5. Use dashboard **Test Buy/Test Sell** before enabling TradingView alerts. Test
   signals authenticate/read broker state but deliberately do not place an order.
6. Stop a foreground bot with Ctrl+C. Use `./bot.sh stop` for the optional service.

`start.sh` creates `venv`, installs pinned requirements when needed, validates all
required keys without printing them, checks port 8000, starts a configured local
Cloudflare tunnel, and launches KAIROS. It never kills an unknown process occupying
port 8000.

## Personal one-click `KAIROS.command` launcher (macOS)

`KAIROS.command` is intentionally **not included or committed**. Each customer makes
their own in the downloaded KAIROS folder so its path and macOS permissions belong
to their machine. The repository ignores `*.command` files.

Create a plain-text file named exactly `KAIROS.command` beside `start.sh` and paste:

```bash
#!/bin/bash
cd "$(dirname "$0")" || exit 1
./start.sh
exit_code=$?
if [ "$exit_code" -ne 0 ]; then
  echo
  read -r -p "KAIROS stopped with an error. Press Return to close."
fi
```

Then run this once from Terminal while inside the KAIROS folder:

```bash
chmod +x KAIROS.command start.sh bot.sh
```

After the one-time setup, double-clicking `KAIROS.command` launches the tunnel (if
configured) and bot in one Terminal window. Keep that window open; Ctrl+C stops the
foreground bot. Never double-click twice, and do not use the shortcut while
`./bot.sh start` is running the background service.

## How to ask an AI assistant for help

Begin a new coding conversation with:

> Read `AGENTS.md`, `HOW-TO-USE.md`, and the relevant source before acting. Do not
> read or print `.env`, start the bot, connect to the broker, change accounts, place
> orders, or push changes unless I explicitly ask. Preserve strict candle-3 alerts,
> attached protection, no-hedge rules, and all existing tests.

Useful requests include:

- “Explain exactly why this webhook was ignored; inspect logs without changing code.”
- “Trace an A+ long from Pine alert through its candle-1 and swing-stop handling.”
- “Add this dashboard setting, update persistence/docs/tests, but do not run the bot.”
- “Review my diff for any way a stale exit or hedge could close the wrong position.”
- “Run the safe test suite and tell me what remains unverified manually.”

For a code change, require the assistant to state assumptions, preserve unrelated
work, update `README.md`/`CHANGELOG.md` when behavior changes, run the relevant tests,
and show the final diff before any push.

## Safety rules for people and AI assistants

- Never commit, paste, screenshot, log, or share `.env` or Cloudflare credential files.
- Never put the real `WEBHOOK_SECRET` directly in `alertbot.pine`; enter it through
  the indicator's private Settings panel.
- Use a practice/simulated account until the full alert → webhook → dashboard path
  has been observed and risk settings have been reviewed.
- Topstep API trading must originate from the customer's permitted personal device;
  do not assume a VPS, VPN, proxy or cloud VM is allowed.
- Only one bot instance may manage an account. Check the dashboard/service before launch.
- Do not change stop placement, sizing, exposure, hedging, cooldowns or exit semantics
  as a “cleanup”; these are trading rules and require explicit owner approval.
- Never run `main.py`, `start.sh`, `bot.sh start`, live webhook tests, account changes,
  flatten controls, or TradingView alert creation during a code-review-only request.
- Pine must still be compiled in TradingView after changes; local Python tests cannot
  prove TradingView compilation or broker behavior.

## Verification before a release

Safe local checks that do not start the bot:

```bash
bash -n start.sh bot.sh
python3 -m compileall -q main.py adaptive_sizing.py Analytics/generate_analytics.py
python3 -m unittest discover -s tests -v
git diff --check
git status --short
```

Also verify that `.env`, state, logs, results, `venv`, tunnel credentials and
`KAIROS.command` are not staged. A release is not customer-ready until the new Pine
compiles in TradingView, its alert is recreated, a practice test passes, and the
customer has explicit access to the private GitHub repository.
