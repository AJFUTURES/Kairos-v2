# How to Use KAIROS

This is the customer walkthrough for a **personal Mac**. Complete the one-time
setup once; afterward, KAIROS can be launched by double-clicking a local
`KAIROS.command` file. The launcher itself is intentionally not distributed.

> **Start with a TopstepX Practice account.** KAIROS can place real futures orders.
> Topstep currently requires API trading to originate from your personal device
> and prohibits VPS, VPN and remote-server order routing. Read the current
> [TopstepX API Access guide](https://help.topstep.com/en/articles/11187768-topstepx-api-access)
> before connecting any non-practice account.

## What is one-time and what is one-click?

The customer must complete these once:

1. Receive/accept access to the private GitHub repository and download KAIROS.
2. Install Python 3.10 or newer.
3. Obtain ProjectX/TopstepX API access and add personal keys to `.env`.
4. Create a stable Cloudflare Tunnel URL for TradingView.
5. Add the Pine indicator and create strict candle-3 TradingView alerts.
6. Create the personal `KAIROS.command` file and review dashboard risk settings.

Afterward, daily launch is one double-click on `KAIROS.command`. It calls
`start.sh`, which validates setup, starts the configured tunnel if necessary,
and starts the bot.

## Repository owner checklist before inviting a customer

The code package can be shared only after the owner completes these release actions:

- Decide and add the intended software license/customer-use terms. With no `LICENSE`
  file, the repository does not grant customers clear redistribution/modification rights.
- Decide whether to retain the current clean-state trading defaults: the bot starts
  unpaused with MNQ enabled and Custom sizing (5 MNQ contracts). The guide prevents
  alerts until dashboard review, but a more conservative paused/50K default is an
  owner product decision.
- Compile the exact committed `alertbot.pine` in TradingView and recreate its alert.
- Complete the Practice-account acceptance test in this guide on a personal Mac.
- Keep the GitHub repository private, then invite the customer's exact GitHub account;
  never send a ZIP containing the owner's `.env`, state, logs or tunnel credentials.
- Decide whether to create a version tag/GitHub Release for an immutable customer build.
- If the optional `site/` landing page is included in the offering, replace its example
  domain/links for that customer. It is not required to operate the bot.
- Tell the customer the support/update policy and that Topstep/ProjectX does not support
  third-party bot implementation or losses caused by it.

The historical Oracle/cloud deployment guide is **not a Topstep deployment option**;
its warning is intentional. Topstep order transmission belongs on the customer's
permitted personal device.

## 1. Get access and download the files

Because the repository is private, the owner must invite the customer's GitHub
account. The customer must accept that invitation before the link works.

Recommended method:

```bash
git clone https://github.com/AJFUTURES/Kairos-v2.git KAIROS
cd KAIROS
```

Non-technical method: on GitHub choose **Code → Download ZIP**, unzip it, rename
the folder `KAIROS`, and move it somewhere permanent. Do not move the folder after
creating the one-click launcher or installing the optional background service.

Never download KAIROS from an unofficial mirror or accept a pre-filled `.env`.

## 2. Install the prerequisites

Install:

- Python **3.10 or newer** from [python.org](https://www.python.org/downloads/macos/).
- A TradingView account that supports webhooks, with **two-factor authentication
  enabled**. TradingView requires 2FA for webhook alerts.
- ProjectX/TopstepX API access and a TopstepX Practice account.
- A domain using Cloudflare DNS and the `cloudflared` utility for a stable HTTPS URL.

Check Python in Terminal:

```bash
python3 --version
```

You do not need to install Python packages manually. `start.sh` creates the
private `venv` folder and installs the pinned packages on its first preflight.

## 3. Obtain the TopstepX API key

Follow Topstep's current API guide in order: create/sign in to the ProjectX
Dashboard, subscribe to API access, link it to the TopstepX profile, then generate
the API key under **TopstepX → Settings → API**.

Record these privately:

- ProjectX username.
- TopstepX API key.
- The **exact active account name** shown by TopstepX/ProjectX. Despite the
  environment variable's historical name, `PROJECT_X_ACCOUNT_ID` expects this
  account name; KAIROS resolves the numeric broker ID itself.

The API key can trade every eligible account under the profile. Never send it by
email/chat, paste it into TradingView, or give it to an AI assistant.

## 4. Create and fill `.env`

Inside the downloaded KAIROS folder, duplicate `.env.example` and rename the copy
to `.env`. In Finder, press **Command + Shift + .** if hidden dot-files are not visible.

Terminal alternative:

```bash
cp .env.example .env
chmod 600 .env
```

Fill the five required values:

```dotenv
PROJECT_X_USERNAME=
PROJECT_X_API_KEY=
PROJECT_X_ACCOUNT_ID=
WEBHOOK_SECRET=
DASHBOARD_TOKEN=
```

Type each personal value after its `=` only in the customer's local `.env`.

Generate each random secret separately:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Optional Discord and Telegram values can remain blank. Do not add quotes unless a
value genuinely contains spaces, and do not add spaces around `=`.

The real `.env` is ignored by Git. `.env.example` must always remain blank.

## 5. Create the stable Cloudflare webhook URL

TradingView must reach KAIROS over public HTTPS. KAIROS itself stays bound to
`127.0.0.1:8000`; Cloudflare Tunnel forwards only the chosen hostname.

Your domain must already use Cloudflare nameservers. Then install `cloudflared`:

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create kairos
```

The last command prints a tunnel UUID and creates a credentials JSON file. Create
`~/.cloudflared/config.yml` with the customer's real UUID, macOS username and domain:

```yaml
url: http://127.0.0.1:8000
tunnel: YOUR-TUNNEL-UUID
credentials-file: /Users/YOUR-MAC-USERNAME/.cloudflared/YOUR-TUNNEL-UUID.json
```

Route a hostname such as `app.example.com`:

```bash
cloudflared tunnel route dns kairos app.example.com
cloudflared tunnel --config "$HOME/.cloudflared/config.yml" run
```

The second command is a temporary test. When it reports connected, press Ctrl+C.
The one-click launcher will start this configured tunnel later. Customers who want
the tunnel to start automatically at login may follow Cloudflare's official
[macOS service guide](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/macos/).

Add the public origin to `.env` so the launcher prints the correct links:

```dotenv
KAIROS_PUBLIC_URL=https://app.example.com
```

Do not use a temporary quick-tunnel URL for permanent TradingView alerts because
the URL changes. Never expose raw port 8000 on the router.

## 6. Run the safe preflight

From Terminal, inside the KAIROS folder:

```bash
chmod +x start.sh bot.sh
./start.sh --check
```

On the first run this creates `venv` and installs dependencies. It validates the
presence of required values without displaying them, checks whether the tunnel is
configured, and checks port 8000. **Preflight does not start the tunnel, connect to
the broker, or place an order.**

Do not continue until it ends with `KAIROS preflight passed`.

## 7. First controlled launch and dashboard review

Do this **before creating any TradingView alert**, so no market signal can arrive
while the customer's account and risk settings are still being reviewed:

```bash
./start.sh
```

The launcher:

1. Revalidates Python, dependencies and required `.env` values.
2. Uses an existing Cloudflare service/tunnel or starts the configured local tunnel.
3. Stops only a prior KAIROS process recorded by this folder.
4. Refuses to kill an unknown application if port 8000 is occupied.
5. Starts KAIROS on `127.0.0.1:8000` and keeps the Mac awake while it runs.

Open the public dashboard URL or local fallback:

```text
https://app.example.com/dashboard
http://127.0.0.1:8000/dashboard
```

Enter `DASHBOARD_TOKEN`. Before any live alert is active, verify:

- The selected account is the intended **Practice** account.
- Account-size profile and contract quantities are correct.
- Only intended instruments are enabled; full-size NQ/ES/GC/CL are off unless deliberate.
- Long/short/both direction is correct for each no-hedge group.
- Structural or Swing stop, take-profit, break-even, minimum FVG and maximum stop are correct.
- A+ enablement, fallback target and per-session cap are correct.
- Macro/1-minute session filters and Adaptive Sizing are correct.
- Both SignalR indicators connect and live positions match the broker.

Use dashboard **Test Buy** and **Test Sell**. These tests authenticate and read
broker state but return before order placement. Confirm the activity feed says what
the bot *would* do. They are not a substitute for a Practice-account end-to-end alert.

Stop the foreground bot with Ctrl+C.

## 8. Add the indicator to TradingView

1. Open `alertbot.pine` from the KAIROS folder.
2. Copy all of it into TradingView's Pine Editor and choose **Add to chart**.
3. Use an intraday chart for a supported symbol: MNQ, NQ, MES, ES, MGC, GC or CL.
4. In indicator **Settings → Alerts**, choose **Webhook JSON** or **Both**.
5. Paste only `WEBHOOK_SECRET` into **Webhook Secret**. Never paste the broker API
   key, ProjectX password, or dashboard token into Pine.
6. Configure the desired session, IFVG, HTF, visual and risk inputs.

The source must compile successfully in TradingView. Pine compilation cannot be
confirmed by the local Python tests.

## 9. Create the strict candle-3 alert

TradingView requires 2FA for webhooks and sends webhooks only to accepted public
ports such as HTTPS/443. Cloudflare supplies that HTTPS endpoint.

For each chart/instrument:

1. Choose **Create Alert**.
2. Condition: **IFVG Pro v7 → Any alert() function call**.
3. Frequency: **Once Per Bar Close**, if shown.
4. Notifications: enable **Webhook URL**.
5. URL: `https://app.example.com/webhook` using the customer's real hostname.
6. Leave the message field unchanged; Pine creates the JSON.
7. Save and confirm the alert is enabled only for the intended Practice test.

> **Delete and recreate every old candle-2 alert.** TradingView stores a snapshot
> of the script and inputs when an alert is created; replacing Pine code on the
> chart does not update an existing alert.

TradingView notes that webhook delivery can occasionally fail. Check the Alert Log's
webhook status when diagnosing a missing signal; see TradingView's official
[webhook guide](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/).

## 10. Create the personal one-click launcher

Do not download a pre-made `.command` file. Create a plain-text file named exactly
`KAIROS.command` beside `start.sh`, containing:

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

Make it executable once:

```bash
chmod +x KAIROS.command start.sh bot.sh
```

Now double-click `KAIROS.command` in Finder. Keep its Terminal window open while
trading; Ctrl+C stops the foreground bot. The file is ignored by Git and must remain
local to that customer. Do not double-click it twice.

For an optional no-Terminal background bot, first complete the foreground setup,
install Cloudflare as a macOS service, then use `./bot.sh start`. Do not run
`bot.sh start` and `KAIROS.command` together.

## 11. Practice-account acceptance test

Before considering the installation ready:

- `https://app.example.com/health` returns `{"status":"ok"}`.
- Dashboard login rejects a wrong token and accepts the real dashboard token.
- SignalR status is connected and the dashboard shows the correct Practice account.
- A dashboard Test Buy/Test Sell logs a dry run and places no order.
- A real TradingView alert reaches the activity feed only after candle 3 closes.
- The received symbol, timeframe, direction, imbalance, A+ status and levels are correct.
- One small Practice trade receives both broker stop and take-profit brackets.
- For A+, initial candle-1 protection and the confirmed transition to swing H/L are
  visible in the logs and broker orders.
- Break-even, close-based exit, no-hedge and orphan cleanup are tested only in Practice.
- Restart recovery shows the same broker position and does not create a second order.

The final items involve a real Practice order and must be performed by the customer;
automated repository tests deliberately do not connect to or trade a broker account.

## Daily use

1. Confirm the Mac is on personal/home internet and will remain awake and online.
2. Confirm no KAIROS background service or second copy is already running.
3. Double-click `KAIROS.command`.
4. Open the dashboard and confirm account, connection, pause state and positions.
5. Keep the Terminal window open. Monitor the TradingView Alert Log and bot feed.
6. Press Ctrl+C when done. The tunnel may remain running; it cannot trade by itself.

## Updating KAIROS

If cloned with Git:

```bash
git pull
./start.sh --check
```

The launcher reinstalls dependencies only when `requirements.txt` changes. Preserve
the local `.env` and `bot_state.json`; neither belongs in Git. After any Pine update,
paste the new script into TradingView and recreate the alerts because old alerts keep
their earlier script snapshot.

## Troubleshooting

### “Permission denied” when launching

```bash
chmod +x KAIROS.command start.sh bot.sh
```

### Missing `.env` values

Open `.env` and fill every required item. `PROJECT_X_ACCOUNT_ID` must be the exact
active account **name**. Do not edit `.env.example` with real values.

### Broker authentication or account not found

Confirm the API subscription is active and linked, generate a current API key, use
the ProjectX username, and copy the exact account name. KAIROS lists available account
names in its local error log without printing the API key.

### Local dashboard does not open

Run `./start.sh --check`. If port 8000 belongs to another application, close it;
KAIROS intentionally will not kill an unknown process. Check the Terminal error.

### Public dashboard/webhook does not open

Confirm `cloudflared tunnel list`, the `config.yml` UUID/credentials path, the DNS
route, and `cloudflared.log`. The local dashboard can work while the tunnel is broken.

### TradingView alert does not arrive

Confirm TradingView 2FA, webhook URL ending `/webhook`, Webhook JSON mode, matching
`WEBHOOK_SECRET`, Any `alert()` function call, the correct chart symbol, enabled alert,
and the Alert Log's webhook status. Remember that KAIROS now waits for candle 3 close.

### Bot receives but ignores the signal

The activity feed states the gate: paused, disabled/unsupported symbol, duplicate,
direction, session/macro, minimum FVG, maximum stop, A+ switch/cap, adaptive lock,
existing exposure, or no-hedge conflict. Ask an AI assistant to trace that message
using `AGENTS.md`; do not share `.env`.

## Files that must never be shared

- `.env`
- `~/.cloudflared/cert.pem`
- `~/.cloudflared/*.json`
- `bot_state.json` and backups
- logs, results and trade exports
- the customer's locally created `KAIROS.command` if it has been modified to contain
  any machine-specific or sensitive command

Everything needed to understand the sanitized code is documented in `AGENTS.md` and
`README.md`. Futures trading remains the customer's responsibility; test completely
on Practice before using an Evaluation or Funded account.
