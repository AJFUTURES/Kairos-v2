#!/usr/bin/env bash
# KAIROS customer launcher.
#
# First run: creates ./venv and installs requirements. Every run validates .env,
# starts a configured Cloudflare named tunnel when available, safely replaces only
# the KAIROS process recorded in .kairos.pid, and launches the bot on localhost.
#
#   ./start.sh          prepare + launch KAIROS in the foreground
#   ./start.sh --check  validate setup without connecting to the broker
#
# KAIROS.command is intentionally not distributed. HOW-TO-USE.md shows each owner
# how to create a two-line local launcher that calls this file.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VENV="$DIR/venv"
PY="$VENV/bin/python"
REQ_STAMP="$VENV/.kairos-requirements"
BOT_PID_FILE="$DIR/.kairos.pid"
TUNNEL_PID_FILE="$DIR/.kairos-tunnel.pid"
TUNNEL_LOG="$DIR/cloudflared.log"
CHECK_ONLY=false

if [ "${1:-}" = "--check" ]; then
  CHECK_ONLY=true
elif [ -n "${1:-}" ]; then
  echo "usage: ./start.sh [--check]"
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[SETUP ERROR] Python 3.10+ is required. Install Python, then try again."
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[SETUP ERROR] Python 3.10+ is required; found $PY_VERSION."
  exit 1
fi

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo "[SETUP REQUIRED] Created .env from .env.example."
  echo "Open $DIR/.env, add your own credentials, then launch KAIROS again."
  exit 1
fi

if [ ! -x "$PY" ]; then
  echo "▸ Creating the private Python environment..."
  python3 -m venv "$VENV"
fi

if [ ! -f "$REQ_STAMP" ] || ! cmp -s "$DIR/requirements.txt" "$REQ_STAMP"; then
  echo "▸ Installing KAIROS dependencies (first run or requirements changed)..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$DIR/requirements.txt"
  cp "$DIR/requirements.txt" "$REQ_STAMP"
fi

MISSING_ENV="$("$PY" -c 'from dotenv import dotenv_values; from pathlib import Path; required=("PROJECT_X_USERNAME","PROJECT_X_API_KEY","PROJECT_X_ACCOUNT_ID","WEBHOOK_SECRET","DASHBOARD_TOKEN"); values=dotenv_values(Path(".env")); print(", ".join(k for k in required if not str(values.get(k) or "").strip()))')"
if [ -n "$MISSING_ENV" ]; then
  echo "[SETUP ERROR] Missing required .env values: $MISSING_ENV"
  echo "Edit $DIR/.env and try again. Secrets were not displayed."
  exit 1
fi

PUBLIC_URL="$("$PY" -c 'from dotenv import dotenv_values; print(str(dotenv_values(".env").get("KAIROS_PUBLIC_URL") or "").strip().rstrip("/"))')"

start_tunnel_if_configured () {
  if command -v launchctl >/dev/null 2>&1 \
      && launchctl list 2>/dev/null | grep -Eq 'com\.(kairos|cloudflare)\.cloudflared'; then
    echo "▸ Cloudflare tunnel: running as a macOS service."
    return
  fi

  if [ -f "$TUNNEL_PID_FILE" ]; then
    tunnel_pid="$(cat "$TUNNEL_PID_FILE" 2>/dev/null || true)"
    if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
      tunnel_cmd="$(ps -p "$tunnel_pid" -o command= 2>/dev/null || true)"
      case "$tunnel_cmd" in
        *cloudflared*)
          echo "▸ Cloudflare tunnel: running (PID $tunnel_pid)."
          return
          ;;
      esac
    fi
    rm -f "$TUNNEL_PID_FILE"
  fi

  if [ "$CHECK_ONLY" = true ]; then
    if command -v cloudflared >/dev/null 2>&1 && [ -f "$HOME/.cloudflared/config.yml" ]; then
      echo "▸ Cloudflare tunnel: configured and ready (not started by preflight)."
    else
      echo "▸ Cloudflare tunnel: not configured; complete HOW-TO-USE.md before alerts."
    fi
    return
  fi

  if command -v cloudflared >/dev/null 2>&1 && [ -f "$HOME/.cloudflared/config.yml" ]; then
    echo "▸ Starting the configured Cloudflare tunnel..."
    nohup cloudflared tunnel --config "$HOME/.cloudflared/config.yml" run \
      >"$TUNNEL_LOG" 2>&1 </dev/null &
    echo "$!" > "$TUNNEL_PID_FILE"
    sleep 1
    if kill -0 "$!" 2>/dev/null; then
      echo "▸ Cloudflare tunnel: started (log: $TUNNEL_LOG)."
      return
    fi
    rm -f "$TUNNEL_PID_FILE"
    echo "[TUNNEL WARNING] cloudflared exited. Check $TUNNEL_LOG."
    return
  fi

  echo "[TUNNEL NOTICE] No configured Cloudflare tunnel was found."
  echo "KAIROS can run locally, but TradingView cannot reach /webhook until the"
  echo "one-time tunnel steps in HOW-TO-USE.md are complete."
}

stop_previous_kairos () {
  if [ ! -f "$BOT_PID_FILE" ]; then
    return
  fi
  old_pid="$(cat "$BOT_PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    old_cmd="$(ps -p "$old_pid" -o command= 2>/dev/null || true)"
    case "$old_cmd" in
      *"$PY"*main.py*) ;;
      *)
        echo "[START NOTICE] Ignoring stale .kairos.pid; PID $old_pid is not this KAIROS."
        rm -f "$BOT_PID_FILE"
        return
        ;;
    esac
    echo "▸ Stopping the previous KAIROS process (PID $old_pid)..."
    kill "$old_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "[START ERROR] Previous KAIROS process did not stop. Use ./bot.sh stop"
      echo "or stop PID $old_pid manually, then try again."
      exit 1
    fi
  fi
  rm -f "$BOT_PID_FILE"
}

port_is_free () {
  "$PY" -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 8000)); s.close()' >/dev/null 2>&1
}

echo "▸ Python $PY_VERSION · environment and required keys validated."
start_tunnel_if_configured

if [ "$CHECK_ONLY" = true ]; then
  if port_is_free; then
    echo "▸ Port 8000 is available."
  else
    echo "▸ Port 8000 is already in use (expected if KAIROS is running)."
  fi
  echo "✓ KAIROS preflight passed. No broker connection was made."
  exit 0
fi

stop_previous_kairos
if ! port_is_free; then
  echo "[START ERROR] Port 8000 is occupied by another application."
  echo "KAIROS did not kill it. Close that application, then try again."
  exit 1
fi

echo "▸ Local dashboard: http://127.0.0.1:8000/dashboard"
if [ -n "$PUBLIC_URL" ]; then
  echo "▸ Webhook: $PUBLIC_URL/webhook"
  echo "▸ Dashboard: $PUBLIC_URL/dashboard"
fi
echo "▸ Starting KAIROS — Ctrl+C stops the foreground bot."
echo "──────────────────────────────────────────────────────────"

echo "$$" > "$BOT_PID_FILE"
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -is "$PY" main.py
fi
exec "$PY" main.py
