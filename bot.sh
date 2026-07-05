#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# KAIROS bot as a BACKGROUND SERVICE — no Terminal window required.
#
# This is an OPTIONAL alternative to ./start.sh. The difference:
#   • ./start.sh  runs the bot in the foreground — closing the Terminal/Ctrl+C
#     kills the bot.
#   • ./bot.sh    runs the bot via a launchd agent (com.kairos.bot) — it keeps
#     running after you close the Terminal, auto-restarts if it crashes, and (if
#     left "on") resumes after a reboot. The public tunnel is separate and always
#     up, exactly as before.
#
# ⚠️  Do NOT use ./start.sh and ./bot.sh at the same time — that's two bots on
#     port 8000. `bot.sh start` first clears any ./start.sh bot, so just pick one.
#
#   ./bot.sh start     turn the bot ON  (stays up across Terminal close/crash/reboot)
#   ./bot.sh stop      turn the bot OFF (and it will NOT auto-start on reboot)
#   ./bot.sh restart   stop then start
#   ./bot.sh status    show bot + tunnel state
#   ./bot.sh logs      tail the bot log
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

LABEL="com.kairos.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"
DIR="$(cd "$(dirname "$0")" && pwd)"              # self-locating: survives folder moves
LOG="$DIR/kairos.log"

write_plist () {
  # (Re)write the launchd plist on EVERY start so the service definition can
  # never drift or go stale. The keys that make the bot effectively unkillable:
  #   KeepAlive=true       → launchd restarts main.py after ANY exit — crash,
  #                          clean exit, OOM kill, unhandled exception. The only
  #                          way it stays down is `./bot.sh stop` (bootout).
  #   RunAtLoad=true       → starts immediately on load and after every reboot/login.
  #   ThrottleInterval=5   → restart within ~5s instead of launchd's 10s default.
  #   caffeinate -is       → no idle/system sleep while the bot runs (AC power);
  #                          a sleeping Mac drops webhooks. (Lid-close on battery
  #                          still sleeps — that's a macOS hardware policy.)
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-is</string>
    <string>$DIR/venv/bin/python</string>
    <string>main.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
}

kill_stray_bot () {
  # Stop any bot started via ./start.sh (foreground) so we never double-run.
  pkill -f "caffeinate -is .*main.py" 2>/dev/null || true
  pkill -f "venv/bin/python main.py" 2>/dev/null || true
  for _ in $(seq 1 20); do pgrep -f "main.py" >/dev/null 2>&1 || break; sleep 0.5; done
  lsof -ti tcp:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
}

case "${1:-}" in
  start)
    echo "▸ Ensuring the Cloudflare tunnel is up..."
    launchctl list 2>/dev/null | grep -q com.kairos.cloudflared \
      || launchctl load -w "$HOME/Library/LaunchAgents/com.kairos.cloudflared.plist" 2>/dev/null || true
    echo "▸ Clearing any foreground (start.sh) bot..."
    kill_stray_bot
    echo "▸ Writing a fresh service definition (KeepAlive: auto-restart on any exit)..."
    write_plist
    echo "▸ Starting bot as background service ($LABEL)..."
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$GUI" "$PLIST"
    sleep 1
    "$0" status
    ;;
  stop)
    echo "▸ Stopping bot service (won't auto-start on reboot)..."
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    lsof -ti tcp:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    echo "  stopped."
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if launchctl print "$GUI/$LABEL" >/dev/null 2>&1; then echo "service : LOADED ($LABEL)"; else echo "service : not loaded"; fi
    if lsof -ti tcp:8000 -sTCP:LISTEN >/dev/null 2>&1; then echo "bot     : RUNNING on 127.0.0.1:8000"; else echo "bot     : not listening on :8000"; fi
    if launchctl list 2>/dev/null | grep -q com.kairos.cloudflared; then echo "tunnel  : running (always-on)"; else echo "tunnel  : NOT running"; fi
    echo "public  : https://your-domain.example   (dashboard /dashboard, webhook /webhook)"
    ;;
  logs)
    tail -f "$LOG"
    ;;
  *)
    echo "usage: ./bot.sh {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
