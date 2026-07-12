# KAIROS

**KAIROS** is an automated futures-trading system built around Inverted Fair Value Gaps (IFVG). It has two halves that talk to each other over a webhook:

1. **A TradingView Pine Script indicator** (`alertbot.pine`) that detects IFVG setups on intraday charts and fires a JSON alert the moment a signal candle closes.
2. **A Python (FastAPI) execution bot** (`main.py`) that receives those alerts, validates and filters them, and places bracket orders on futures through the **ProjectX / TopstepX API** — with live fill tracking over SignalR websockets, structural (close-based) invalidation exits, per-instrument risk filters, Discord/Telegram trade notifications, persistent state, and a token-protected web dashboard for live control (pause, position sizing, instrument toggles, presets, account switching, flatten-all).

Supported instruments: **MNQ / NQ, MES / ES, MGC / GC, CL** (micro and full-size Nasdaq, S&P, Gold, and Crude futures).

![KAIROS dashboard in action](assets/dashboard.png)

> **Note on the included Pine script:** `alertbot.pine` in this repository is a public reference version — it detects IFVGs and fires entry alerts. The advanced signal-side features (swing-stop placement, structural invalidation exit alerts, A+ setup detection against higher-timeframe FVG draws) are not included. Those alert fields are optional, so the bot works with this script as-is and falls back to its own stop/target logic.

---

## Requirements

- **Python 3.10+** (dependencies in `requirements.txt`: FastAPI, Uvicorn, httpx, websockets, python-dotenv, certifi)
- **A TradingView account with alert/webhook capability** (webhook alerts require a paid TradingView plan)
- **ProjectX / TopstepX API access** — an account on a ProjectX-powered platform (e.g. TopstepX) with API access enabled
- **A publicly reachable HTTPS URL** for the webhook — the bot binds to `127.0.0.1:8000`, so you need a tunnel (Cloudflare Tunnel is what the included scripts assume) or a server with a reverse proxy

## Getting your ProjectX credentials

1. Log in to your ProjectX-powered platform (e.g. TopstepX).
2. Generate an API key — see [TopstepX API Access](https://help.topstep.com/en/articles/11187768-topstepx-api-access) (a one-time API access purchase may be required).
3. Note your platform **username** and the **name/ID of the account** you want the bot to trade (practice/eval/funded — the bot can also switch accounts live from the dashboard).

## Setup

```bash
# 1. Clone / download this repository
git clone <your-repo-url> KAIROS && cd KAIROS

# 2. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure your environment
cp .env.example .env
# then open .env and fill in every REQUIRED value
```

### .env variables

| Variable | Required | What it is |
|---|---|---|
| `PROJECT_X_USERNAME` | ✅ | Your ProjectX/TopstepX platform username |
| `PROJECT_X_API_KEY` | ✅ | Your ProjectX API key |
| `PROJECT_X_ACCOUNT_ID` | ✅ | The account name/ID the bot trades by default |
| `WEBHOOK_SECRET` | ✅ | A long random string; every TradingView alert must include it — the bot rejects anything else |
| `DASHBOARD_TOKEN` | ✅ | A second long random string; required to open the dashboard and call control endpoints |
| `DISCORD_WEBHOOK_URL` | optional | Discord webhook for trade notifications |
| `DISCORD_MENTION` | optional | `everyone`, `here`, or a numeric user ID to ping on A+ / auto-pause alerts |
| `TELEGRAM_BOT_TOKEN` | optional | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | optional | Telegram chat ID to message |

Generate the two secrets with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The bot **refuses to start** if any required variable is missing.

## Running the bot

```bash
./start.sh          # foreground — Ctrl+C stops the bot
# or
./bot.sh start      # macOS background service via launchd (survives Terminal close & reboot)
./bot.sh status|logs|stop
```

Or directly: `./venv/bin/python main.py`. The bot listens on `127.0.0.1:8000`:

- `POST /webhook` — TradingView alerts come in here
- `GET /dashboard?token=<DASHBOARD_TOKEN>` — live control panel
- `GET /health` — health check

To expose it publicly, set up a Cloudflare Tunnel mapping `https://app.<your-domain>` → `127.0.0.1:8000`. For an always-on cloud deployment (Oracle Always-Free VM, systemd units included), see [`deploy/DEPLOY_ORACLE.md`](deploy/DEPLOY_ORACLE.md) and [`deploy/DEPLOY_LANDING.md`](deploy/DEPLOY_LANDING.md).

## Dashboard settings

The dashboard (`/dashboard?token=<DASHBOARD_TOKEN>`) is the bot's live control panel. Every change takes effect immediately and persists across restarts. The cards:

- **Account** — switch the active broker account on the fly (practice / eval / funded); shows account size, current contract sizing, session stats, and the active stop/TP scheme.
- **Trade Settings** — choose the stop mode (Structural or Swing), set take-profit as a multiple of the stop or as flat points per instrument group, enable auto break-even (stop → entry at 50% of TP), and restrict entries to the macro window only.
- **Instruments** — tick each instrument on/off (micro MNQ · MES · MGC or mini NQ · ES · GC · CL) and click to cycle a per-symbol direction bias (long / short / both).
- **Filters & Risk** — per-group Minimum FVG size (ticks) and Maximum Stop cap; configure the 1-minute session window (5m signals always trade, A+ any time) and the macro-window width (± minutes around each hour).
- **A+ Setups** — master switch for A+ trades, fallback take-profit distances used when the alert carries no HTF FVG level, and a max-A+-per-session risk guard.
- **Presets** — snapshot the entire current configuration as a named preset; apply or delete presets in one click.
- **Testing** — fire simulated buy/sell signals and test notifications without touching the market, plus the live trade log, activity feed, and closed-trade results table.

## TradingView alert setup

Alerts flow: **TradingView chart → alert() → your webhook URL → bot**.

1. Open `alertbot.pine`, replace `YOUR_WEBHOOK_SECRET` (2 occurrences) with the exact `WEBHOOK_SECRET` from your `.env`, then add the indicator to your chart (Pine Editor → paste → Add to chart). Use an intraday chart (e.g. 1m/3m/5m) of a supported instrument.
2. Create an alert: **Alerts → Create Alert**, Condition = **KAIROS** → **Any alert() function call**.
3. In **Notifications**, enable **Webhook URL** and set it to your public webhook endpoint, e.g. `https://app.<your-domain>/webhook`.
4. Leave the message field alone — the script builds the JSON payload itself.
5. Repeat per chart/instrument you want the bot to trade, and make sure that instrument is ticked ON in the bot dashboard.

The alert payload looks like:

```json
{"secret": "...", "symbol": "MNQ", "action": "buy", "timeframe": "3m",
 "ifvg_type": "Bullish", "sl_price": 21510.25, "entry_ref": 21522.50, "imbalance": 18.0}
```

Optional fields the bot also understands (sent by the full private script): `swing_sl`, `c1_high`, `c1_low`, `a_plus`, `a_plus_target`, and `action: "exit"` for structural invalidation exits. When absent, the bot falls back to its own stop and target logic.

## Repository layout

```
main.py            The bot — webhook, filters, execution, dashboard API
alertbot.pine      TradingView IFVG indicator (public reference version)
requirements.txt   Python dependencies
start.sh           Foreground runner (macOS/Linux)
bot.sh             macOS launchd background service manager
web/               Dashboard + login pages served by the bot
site/              Static landing page (optional, e.g. Cloudflare Pages)
deploy/            systemd units + cloud/tunnel deployment guides
Analytics/         Trade-analytics dashboard generator (reads results.txt)
.env.example       Template for your .env
```

## Like KAIROS? There's more.

This repo is the free, self-hosted core — and it always will be. The full KAIROS experience lives at **[OnlyRules](https://onlyrules.online)**, the rule-based trading community it was built inside:

- **IFVG Pro** — the full private signal engine: swing-stop placement, structural invalidation exits, and A+ setup detection against higher-timeframe FVG draws.
- **Custom bot builds** — 1-on-1 with the builder: your risk rules, your position sizing, your instruments.
- **The community** — rule-based traders holding each other to their own rules.

Setups come and go. Edges decay. Discipline compounds. **Markets change — OnlyRules survive.**

→ [onlyrules.online](https://onlyrules.online) · [kairosnow.online](https://kairosnow.online)

## ⚠️ Disclaimer

This is trading software. Futures trading involves substantial risk of loss and is not suitable for everyone. This project is provided **as-is, without warranty of any kind** — you run it entirely **at your own risk**, and you are solely responsible for any trades it places and any losses that result. Nothing in this repository is financial advice. Test on a practice/simulated account first.
