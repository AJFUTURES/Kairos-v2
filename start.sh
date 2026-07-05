#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# KAIROS launcher — one command, one terminal.
#
#   ./start.sh        (Re)start the bot. The public tunnel runs independently.
#
# PUBLIC SITE : https://your-domain.example   (Cloudflare PAGES — always up, this bot
#               does NOT serve it, so visitors see the landing page even when the
#               bot/laptop is off).
# BOT ORIGIN  : https://app.your-domain.example (Cloudflare Tunnel → 127.0.0.1:8000)
#   • Webhook   : https://app.your-domain.example/webhook
#   • Dashboard : https://app.your-domain.example/dashboard
#
# KEY DESIGN: the tunnel is a Cloudflare named tunnel ("kairos") run by a login
# LaunchAgent (com.kairos.cloudflared). It auto-starts, self-heals (KeepAlive),
# and survives reboots — completely independent of the bot. The public landing
# page is hosted separately on Cloudflare Pages, so it is ALWAYS available; only
# the dashboard + webhook (on app.your-domain.example) depend on the bot running.
# The app domain is fixed, so the TradingView webhook URL never changes again.
#
#   • Ctrl+C stops the BOT only. The tunnel keeps running.
#   • Tunnel controls:
#       launchctl list | grep kairos
#       launchctl unload ~/Library/LaunchAgents/com.kairos.cloudflared.plist   # stop
#       launchctl load -w ~/Library/LaunchAgents/com.kairos.cloudflared.plist  # start
#       tail -f ~/.cloudflared/cloudflared.log
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"

if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi

# ── 1. Ensure the Cloudflare tunnel is up (managed by launchd, not by us) ─────
if launchctl list 2>/dev/null | grep -q com.kairos.cloudflared; then
  echo "▸ Cloudflare tunnel: running (LaunchAgent com.kairos.cloudflared)."
else
  echo "▸ Cloudflare tunnel: not loaded — loading it..."
  launchctl load -w ~/Library/LaunchAgents/com.kairos.cloudflared.plist 2>/dev/null || \
    echo "  (!) could not load the tunnel agent — check ~/Library/LaunchAgents/com.kairos.cloudflared.plist"
fi
echo "▸ Public site : https://your-domain.example        (Cloudflare Pages — always up)"
echo "▸ Webhook URL : https://app.your-domain.example/webhook"
echo "▸ Dashboard   : https://app.your-domain.example/dashboard"

# ── 2. (Re)start the bot ONLY — never touches the tunnel ──────────────────────
echo "▸ Restarting KAIROS bot..."
pkill -f "main:app" 2>/dev/null || true
pkill -f "main.py"  2>/dev/null || true
# WAIT for the old bot to actually exit before starting a new one. A plain
# `sleep 1` is not enough — the bot can release port 8000 while still finishing
# SignalR cleanup, letting a second process start alongside it (two BE monitors
# racing on one account). Poll until it's gone, then force-kill any holdout.
for _ in $(seq 1 20); do pgrep -f "main.py" >/dev/null 2>&1 || break; sleep 0.5; done
pkill -9 -f "main.py" 2>/dev/null || true
# Kill only the process LISTENING on 8000 (the bot). The tunnel forwards to 8000
# but does not listen on it, so this can never take the tunnel down.
lsof -ti tcp:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
sleep 1

echo "▸ Bot starting — Ctrl+C stops the BOT (tunnel stays up via launchd)."
echo "──────────────────────────────────────────────────────────"
# Wrap in `caffeinate` so the Mac will NOT idle/system-sleep while the bot runs
# (-i: no idle sleep, -s: no system sleep on AC). Sleeping = dropped webhooks /
# no order management. caffeinate exits automatically when the bot exits.
exec caffeinate -is "$PY" main.py
