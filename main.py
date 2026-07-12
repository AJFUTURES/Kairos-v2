import asyncio
import httpx
import hmac
import logging
import os
import time
import json
import websockets
import ssl
import certifi
import adaptive_sizing as adaptive   # adaptive position-sizing engine (micros only)
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, Request, Header, Query, Cookie, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# ─── KAIROS Icon (served from logo.png — monochrome brand mark) ──────────────
import pathlib as _pathlib
_ICON_PNG = (_pathlib.Path(__file__).parent / "logo.png").read_bytes()


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.get("/favicon.png")
@app.get("/logo.png")
async def serve_icon():
    return Response(content=_ICON_PNG, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


USERNAME     = os.getenv("PROJECT_X_USERNAME")
API_KEY      = os.getenv("PROJECT_X_API_KEY")
ACCOUNT_NAME = os.getenv("PROJECT_X_ACCOUNT_ID")   # default account (from .env)

# The active account can be switched live from the dashboard (persisted in state).
# available_accounts is refreshed from the broker on every auth.
active_account     = ACCOUNT_NAME
available_accounts: list = []

# ─── Auth secrets (REQUIRED — bot refuses to act if unset) ────────────────────
# WEBHOOK_SECRET  : shared secret TradingView must include in every alert
# DASHBOARD_TOKEN : token required to open the dashboard / call control endpoints
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN")

# ─── Push alerts on A+ setups (Telegram and/or Discord) ───────────────────────
# Each channel fires the instant an A+ setup is taken, independently. Fire-and-
# forget — a push failure never affects trading. Leave a channel's vars blank to
# disable it. Set in .env:
#   Discord  → DISCORD_WEBHOOK_URL   (Server → Channel → Integrations → Webhooks)
#              DISCORD_MENTION (optional) → who to ping on A+ / auto-pause alerts:
#              "everyone", "here", or a numeric user id. Blank = post with no ping.
#   Telegram → TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_MENTION     = os.getenv("DISCORD_MENTION")

# ─── Startup validation: required env vars ────────────────────────────────────
_REQUIRED_ENV = {
    "PROJECT_X_USERNAME":   USERNAME,
    "PROJECT_X_API_KEY":    API_KEY,
    "PROJECT_X_ACCOUNT_ID": ACCOUNT_NAME,
    "WEBHOOK_SECRET":       WEBHOOK_SECRET,
    "DASHBOARD_TOKEN":      DASHBOARD_TOKEN,
}
_missing_env = [name for name, val in _REQUIRED_ENV.items() if not val]
if _missing_env:
    print(f"[STARTUP ERROR] Missing required environment variables: {', '.join(_missing_env)}")
    print("Set these in your .env file and restart.")
    raise SystemExit(1)

# ─── Constants ───────────────────────────────────────────────────────────────
STATE_FILE     = "bot_state.json"
MIN_SL_TICKS   = 8      # sanity floor: never less than 8 ticks of stop
# ─── Minimum FVG (imbalance) size filter ──────────────────────────────────────
# A buy/sell qualifies only if the Pine-measured inverted-FVG gap ("imbalance",
# sent in TICKS) is at least the floor for that instrument's group. Three groups:
#   • Nasdaq  (MNQ / NQ)             — fast, wide range → bigger gap (16t = 4pt)
#   • S&P+Gold (MES / ES / MGC / GC) — tighter (4t = 1pt on the index minis)
#   • Crude    (CL)                  — 0.01 tick → 10t = $0.10 imbalance floor
# The threshold is in TICKS so it's correct per instrument regardless of $/tick.
# All values are dashboard-editable ("Minimum FVG Ticks") and persisted.
MIN_FVG_GROUPS = {
    "nasdaq":  {"label": "NQ / MNQ",            "symbols": ["MNQ", "NQ"],              "default": 16},
    "sp_gold": {"label": "MES / ES / MGC / GC", "symbols": ["MES", "ES", "MGC", "GC"], "default": 4},
    "crude":   {"label": "CL",                  "symbols": ["CL"],                     "default": 10},
}
_SYMBOL_FVG_GROUP = {s: g for g, meta in MIN_FVG_GROUPS.items() for s in meta["symbols"]}
# Live, dashboard-overridable thresholds (ticks). Mutated in place on load/set.
min_fvg_ticks = {g: meta["default"] for g, meta in MIN_FVG_GROUPS.items()}


def min_fvg_ticks_for(symbol: str) -> float:
    """Minimum qualifying imbalance (ticks) for this instrument's group; 0 if the
    symbol isn't in a configured group (no filter)."""
    g = _SYMBOL_FVG_GROUP.get(symbol)
    return float(min_fvg_ticks.get(g, 0)) if g else 0.0


def tick_for(symbol: str) -> float:
    """Live tick size for a symbol, from the resolved contract. Falls back to a
    sane default when the contract isn't resolved yet (Gold ticks 0.1, Crude 0.01,
    the index futures 0.25). Used by the webhook filters that run before place_order."""
    t = (contracts.get(symbol) or {}).get("tick")
    if isinstance(t, (int, float)) and t > 0:
        return float(t)
    if symbol in ("GC", "MGC"):
        return 0.1
    if symbol == "CL":
        return 0.01
    return 0.25


# ─── Max stop-distance cap (swing-stop guard) ─────────────────────────────────
# When Swing Stop is on, the protective stop is the 15-bar low/high sent by Pine
# (`swing_sl`). If that level is further than this many TICKS from the entry, the
# trade is SKIPPED — a too-wide swing = oversized single-trade risk. Per-group
# (same groups as the FVG filter), dashboard-editable, persisted. 0 = no cap.
# Crude: 50t = $0.50 = $500 max single-trade swing risk on CL ($10/tick).
MAX_STOP_DEFAULTS = {"nasdaq": 60, "sp_gold": 40, "crude": 50}
max_stop_ticks = {g: MAX_STOP_DEFAULTS.get(g, 0) for g in MIN_FVG_GROUPS}


def max_stop_ticks_for(symbol: str) -> float:
    g = _SYMBOL_FVG_GROUP.get(symbol)
    return float(max_stop_ticks.get(g, 0)) if g else 0.0


# ─── Cooldown policy ──────────────────────────────────────────────────────────
# There is exactly ONE cooldown in KAIROS, and it is NOT user-tunable: the global
# circuit breaker (see "Circuit breaker" section) — 3 consecutive losing closes
# across ALL instruments pause the whole bot for 10 minutes, then trading resumes
# automatically. The old per-instrument re-entry cooldown (time/range guards) and
# the A+ post-loss cooldown were removed by design.


def _seconds_since(ts) -> float:
    """Seconds elapsed since an ISO-8601 timestamp (broker creationTimestamp), or
    None if it can't be parsed. Tolerates a trailing 'Z' and >6-digit fractions."""
    if not ts:
        return None
    try:
        s = str(ts).strip().replace("Z", "+00:00")
        # fromisoformat (3.10) accepts at most 6 fractional digits — trim extras.
        import re as _re
        s = _re.sub(r"(\.\d{6})\d+", r"\1", s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None

# ─── 1-minute session gating ──────────────────────────────────────────────────
# 5m IFVGs are always taken. 1m IFVGs are gated by a session mode:
#   "ETH" — take 1m all session (electronic trading hours)
#   "NY"  — take 1m only during the NY session (09:30–16:00 New York)
# Dashboard-editable, persisted. (A+ setups override this — taken any time.)
TF1M_SESSION_OPTIONS = ("ETH", "NY")
tf1m_session = "ETH"

# Macro window width (minutes): the first N AND last N minutes of each NY hour count
# as "macro". Dashboard-editable (5/10/15), persisted. Used by the global Macro Time
# toggle (in_macro_window). Default 10 (= first/last 10 min, i.e. :00–:10 / :50–:00 NY).
MACRO_WINDOW_OPTIONS = (5, 10, 15)
macro_window = 10

IST            = timezone(timedelta(hours=5, minutes=30))
# New York / exchange clock for the Macro Time window (handles US DST via zoneinfo;
# falls back to a fixed UTC-4 if the tz database is unavailable).
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    NY_TZ = timezone(timedelta(hours=-4))


def in_macro_window(now_utc: datetime = None) -> bool:
    """True if the current New York minute-of-hour is within the first `macro_window`
    or last `macro_window` minutes — the Macro Time trading window."""
    n = now_utc or datetime.now(timezone.utc)
    m = n.astimezone(NY_TZ).minute
    return m < macro_window or m >= (60 - macro_window)


def in_ny_rth(now_utc: datetime = None) -> bool:
    """True during the NY cash session — 09:30 to 16:00 New York time."""
    n = (now_utc or datetime.now(timezone.utc)).astimezone(NY_TZ)
    mins = n.hour * 60 + n.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


def tf1m_allowed(now_utc: datetime = None) -> bool:
    """Whether a 1-minute signal may trade right now, per the 1m session mode."""
    return True if tf1m_session == "ETH" else in_ny_rth(now_utc)
SIGNALR_USER_HUB    = "wss://rtc.topstepx.com/hubs/user"
SIGNALR_MARKET_HUB  = "wss://rtc.topstepx.com/hubs/market"
SIGNALR_SEP         = "\x1e"
POLL_INTERVAL       = 30    # seconds between fallback position polls (see notes)
MONTH_CODES         = "FGHJKMNQUVXZ"   # futures month codes (used to match contracts)

# Tradable instruments. Mostly micros; NQ is the FULL-SIZE E-mini Nasdaq (10×
# MNQ, ~$20/pt) — same underlying index as MNQ, so size positions accordingly.
# tick size / point value are resolved live from the broker (Contract/search),
# so this only needs the user-facing label per ticker. You tick which ones to
# trade; a signal executes only if its instrument is ticked. The resolver matches
# on name prefix + month code, so NQ and MNQ never get confused. Order = panel order.
INSTRUMENTS = {
    "MNQ": {"label": "MNQ (Micro Nasdaq)"},
    "NQ":  {"label": "NQ (E-mini Nasdaq)"},
    "MES": {"label": "MES (Micro S&P)"},
    "ES":  {"label": "ES (E-mini S&P)"},
    "MGC": {"label": "MGC (Micro Gold)"},
    "GC":  {"label": "GC (Gold)"},
    "CL":  {"label": "CL (Crude Oil)"},
}

# Contract tiers — the dashboard's "Micro"/"Mini" group toggles flip a whole tier
# at once. Micros are the M-prefixed contracts; the "minis" are their full-size
# siblings (E-mini index futures + full GC gold + full CL crude). _SYMBOL_TIER maps each ticker to
# its tier so the per-instrument state payload can tell the UI which group it's in.
CONTRACT_TIERS = {
    "micro": ["MNQ", "MES", "MGC"],
    "mini":  ["NQ", "ES", "GC", "CL"],
}
_SYMBOL_TIER = {s: t for t, syms in CONTRACT_TIERS.items() for s in syms}

# No-hedge groups: instruments within the same group can never hold opposing
# positions. MNQ/NQ/MES/ES are all US equity index futures and share a directional
# bias; MGC/GC are both gold (micro + full) and likewise can't oppose each other.
NO_HEDGE_GROUPS = [
    {"MNQ", "NQ", "MES", "ES"},
    {"MGC", "GC"},
]


def hedge_group(symbol: str) -> set:
    for grp in NO_HEDGE_GROUPS:
        if symbol in grp:
            return grp
    return {symbol}

# Verify TopstepX WSS certs against the certifi CA bundle. (The old code
# disabled verification to work around a macOS system-cert issue, but that
# allowed any MITM to read the bearer access_token carried on these sockets.)
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# One pooled HTTP client shared by ALL TopstepX REST calls. Keep-alive reuses
# the TCP+TLS connection between requests, so the per-call handshake the old
# per-operation `async with httpx.AsyncClient()` paid (~100-300ms) disappears
# from the signal→order path. Closed on app shutdown.
HTTP = httpx.AsyncClient(
    timeout=10,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)


@asynccontextmanager
async def api_client():
    """Drop-in replacement for the old per-call `httpx.AsyncClient()` context
    managers — hands out the shared pooled client instead of building one."""
    yield HTTP


async def tg_notify(text: str):
    """Push a Telegram message to the configured chat. Fire-and-forget: any
    failure (or missing config) is swallowed so it can never affect trading.
    Call via asyncio.create_task(tg_notify(...)) so it never blocks the loop."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        await HTTP.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=8,
        )
    except Exception:
        pass


# Discord embed colors (decimal ints).
_DISCORD_COLORS = {
    "aplus": 0xF1C40F,   # gold    — A+ entry
    "win":   0x2ECC71,   # green   — TP / profit
    "loss":  0xE74C3C,   # red     — stop / loss
    "be":    0x95A5A6,   # grey    — break-even
    "warn":  0xE67E22,   # orange  — circuit breaker / halt
    "info":  0x5865F2,   # blurple — startup / status
}


def _embed(title, color_key, fields=None, footer=None):
    """Build a Discord embed dict. fields = list of (name, value) tuples (inline)."""
    e = {"title": str(title)[:256], "color": _DISCORD_COLORS.get(color_key, 0x95A5A6)}
    if fields:
        e["fields"] = [{"name": (str(n)[:256] or "—"), "value": (str(v)[:1024] or "—"), "inline": True}
                       for n, v in fields]
    if footer:
        e["footer"] = {"text": str(footer)[:2048]}
    return e


def _discord_mention():
    """(prefix, allowed_mentions) for DISCORD_MENTION = everyone / here / <user id>.
    Mentions live in `content` (an embed's text never pings); allowed_mentions must
    explicitly opt in or Discord silently strips the ping."""
    m = (DISCORD_MENTION or "").strip().lstrip("@").lower()
    if m in ("everyone", "here"):
        return f"@{m} ", {"parse": ["everyone"]}   # one flag governs @everyone AND @here
    if m.isdigit():
        return f"<@{m}> ", {"users": [m]}
    return "", {"parse": []}


async def discord_notify(text: str = None, *, embed: dict = None, mention: bool = False):
    """Push to the configured Discord webhook. With `embed`, posts a rich card;
    otherwise posts plain `text`. `mention=True` prepends the configured DISCORD_MENTION
    (everyone / here / user id) to the content so it actually pings. Fire-and-forget:
    any failure (or missing config) is swallowed so it can never affect trading."""
    if not DISCORD_WEBHOOK_URL:
        return
    prefix, allowed = _discord_mention() if mention else ("", {"parse": []})
    content = prefix
    payload = {"allowed_mentions": allowed}
    if embed is not None:
        payload["embeds"] = [embed]
    elif text:
        content += text
    if content:
        payload["content"] = content
    try:
        await HTTP.post(DISCORD_WEBHOOK_URL, json=payload, timeout=8)
    except Exception:
        pass


async def notify_push(text: str, *, embed: dict = None, mention: bool = False):
    """Fan an alert out to every configured channel: Telegram gets the plain `text`,
    Discord gets the rich `embed` (or `text` if none) plus an optional @mention.
    Each no-ops if unconfigured and swallows its own errors, so this never raises."""
    await asyncio.gather(
        tg_notify(text),
        discord_notify(text, embed=embed, mention=mention),
    )


def _fire_push(text, *, embed=None, mention=False):
    """Schedule notify_push() from sync code. No-op if there's no running loop."""
    try:
        asyncio.create_task(notify_push(text, embed=embed, mention=mention))
    except RuntimeError:
        pass


# Trading modes are now thin PRESETS over the live Trade Settings (TP.1 / TP.2,
# structural-SL toggle, auto-BE toggle). Selecting a mode loads its preset into
# trade_settings; the user can then tweak any setting live until the next mode
# change. Only three modes exist.
MODES = {
    "scalp_5pt":    {"emoji": "\U0001FA99", "label": "5pt Scalp"},
    "bracket":      {"emoji": "❗",          "label": "Bracket"},
    "bracket_cons": {"emoji": "\U0001F6DF", "label": "Cons Bracket"},
}

# TP is two shared settings, split by instrument group:
#   TP.1 → MGC & MES        TP.2 → NQ & MNQ
TP1_SYMBOLS = ("MGC", "MES", "GC", "ES")
TP2_SYMBOLS = ("NQ", "MNQ")
TP1_OPTIONS = [5, 10, 15]
TP2_OPTIONS = [10, 20, 25, 30, 40, 50]

# Crude (CL) sits in neither TP group — its "points" are $1.00 moves at $1,000/pt,
# a different scale from the index/gold minis. It uses its own base flat TP, set to
# match the full-size index/gold base TPs in DOLLARS: 0.8pt × $1,000 = $800, on par
# with NQ's 40pt=$800 (cf. GC's 10pt=$1,000). This is only the flat ("default") TP;
# the N×-stop TP modes (2x/3x/…) are scale-independent and the preferred crude lever.
CRUDE_SYMBOLS = ("CL",)
CRUDE_TP_PTS  = 0.8

# Catastrophe / safety-net hard stop at the broker (points). On a structural trade
# this is ONLY a backstop — the real exit is the close-beyond-candle-1 flatten fired
# by the Pine invalidation alert. It is also the fixed stop for Bracket / 5pt Scalp
# when Structural SL is toggled OFF. (Cons Bracket's fixed stop is symmetric = TP.)
SAFETY_NET_PTS = {"MNQ": 40, "NQ": 40, "MES": 20, "ES": 20, "MGC": 20, "GC": 20, "CL": 1.5}

# ─── A+ Setups — dashboard-editable settings (the "A+ Setups" card) ───────────
# A+ trades take a structural stop and a TP that rides to the near edge of the
# unfilled HTF FVG (a_plus_target from Pine). These defaults back every A+ control;
# the live values live in trade_settings, validated by _coerce_aplus().
#   aplus_enabled        : master switch — when OFF, A+-flagged signals are skipped.
#   aplus_tp_nasdaq      : fixed TP (pts) for NQ/MNQ when Pine sends no usable FVG level.
#   aplus_tp_spgold      : fixed TP (pts) for MES/ES/MGC/GC in that same fallback case.
#   aplus_max_session    : max A+ entries per NY session (0 = unlimited).
APLUS_DEFAULTS = {
    "aplus_enabled": True, "aplus_tp_nasdaq": 50, "aplus_tp_spgold": 15,
    "aplus_max_session": 0,
}


def aplus_tp_fallback_pts(symbol: str):
    """Fixed A+ TP fallback (pts) for a symbol, from the live A+ settings.
    Crude has no A+ fallback field — it falls back to its base flat TP."""
    if symbol in CRUDE_SYMBOLS:
        return CRUDE_TP_PTS
    key = "aplus_tp_nasdaq" if symbol in TP2_SYMBOLS else "aplus_tp_spgold"
    try:
        return max(1, int(trade_settings.get(key, APLUS_DEFAULTS[key])))
    except (ValueError, TypeError):
        return APLUS_DEFAULTS[key]


def _coerce_aplus(ts: dict) -> None:
    """Fill/validate the four A+ settings on a settings dict, in place."""
    ts["aplus_enabled"] = bool(ts.get("aplus_enabled", APLUS_DEFAULTS["aplus_enabled"]))
    ts.pop("aplus_cooldown_losses", None)   # legacy A+ cooldown — removed
    ts.pop("aplus_cooldown_min", None)
    for k in ("aplus_tp_nasdaq", "aplus_tp_spgold", "aplus_max_session"):
        try:
            ts[k] = max(0, int(ts.get(k, APLUS_DEFAULTS[k])))
        except (ValueError, TypeError):
            ts[k] = APLUS_DEFAULTS[k]

# Dollar value of a 1-point move per contract — used for the account-size risk
# tooltip. (The live broker point_value is authoritative when a contract is
# resolved; this is the static fallback so the tooltip works for any instrument.)
POINT_VALUES = {"MNQ": 2.0, "NQ": 20.0, "MES": 5.0, "ES": 50.0, "MGC": 10.0, "GC": 100.0, "CL": 1000.0}

# Take-profit options. "default" = use the flat TP.1/TP.2 point value (editable on
# the fly); "2x".."7x" = TP at N× the active protective-stop distance (swing,
# candle-1, or safety net).
STRUCT_TP_OPTIONS = ("default", "2x", "3x", "5x", "7x")
STRUCT_TP_MULT    = {"2x": 2, "3x": 3, "5x": 5, "7x": 7}

# The two protective-stop modes — MUTUALLY EXCLUSIVE, exactly one is active
# (see _enforce_stop_mode). In point terms:
#   structural_sl : two-phase candle-1 IFVG stop — hard touch-stop during the entry
#                   candle, then close-based (wicks allowed) afterwards.
#   swing_stop    : plain hard stop at the 15-bar swing low/high for the whole trade.
# (Hybrid was removed 2026-06-22 — any persisted hybrid_stop is ignored and degrades
#  to structural via _enforce_stop_mode.)
STOP_MODE_KEYS = ("structural_sl", "swing_stop")

# Each mode's default Trade Settings — legacy preset scaffolding. Trading Mode was
# retired from the UI; stop mode + structural_tp are now configured directly. Kept so
# load_state / presets still seed sane defaults. macro_time: only trade the first &
# last 15 min of each hour (NY/exchange clock).
MODE_PRESETS = {
    "scalp_5pt":    {"tp1": 5,  "tp2": 10, "structural_sl": False, "auto_be": False, "macro_time": False, "structural_tp": "3x", "swing_stop": True},
    "bracket":      {"tp1": 10, "tp2": 40, "structural_sl": False, "auto_be": False, "macro_time": False, "structural_tp": "3x", "swing_stop": True},
    "bracket_cons": {"tp1": 5,  "tp2": 20, "structural_sl": False, "auto_be": False, "macro_time": False, "structural_tp": "3x", "swing_stop": True},
}


def _enforce_stop_mode(ts: dict, prefer: str = None) -> dict:
    """Force exactly ONE stop mode on (structural_sl / swing_stop).
    If both are set, keep `prefer` when it's among them, else swing (the old
    swing-wins default). If none are set, default to structural (or `prefer`).
    Also clears any legacy hybrid_stop key. Mutates and returns `ts`."""
    ts.pop("hybrid_stop", None)   # legacy mode — removed 2026-06-22
    on = [k for k in STOP_MODE_KEYS if ts.get(k)]
    if prefer in on:
        winner = prefer
    elif len(on) == 1:
        winner = on[0]
    elif on:
        winner = next((k for k in ("swing_stop", "structural_sl") if k in on), on[0])
    else:
        winner = prefer if prefer in STOP_MODE_KEYS else "structural_sl"
    for k in STOP_MODE_KEYS:
        ts[k] = (k == winner)
    return ts


def _stop_mode_label(ts: dict) -> str:
    """Human label for the active stop mode in a settings dict."""
    if ts.get("swing_stop"):
        return "Swing"
    return "Structural"

# Live, user-overridable trade settings. Starts on the Bracket preset (+ A+ defaults);
# load_state may replace it with the persisted set.
trade_settings = {**MODE_PRESETS["bracket"], **APLUS_DEFAULTS}


def tp_pts_for(symbol: str) -> float:
    """TP distance (points) for a symbol, from the live trade settings."""
    if symbol in CRUDE_SYMBOLS:
        return CRUDE_TP_PTS
    return trade_settings["tp1"] if symbol in TP1_SYMBOLS else trade_settings["tp2"]

# ─── Break-Even Config ────────────────────────────────────────────────────────
# Auto break-even is now a per-trade toggle in trade_settings["auto_be"] (BE at 50%
# of TP). When off, BE is never armed.
# The BE trigger tracks the LIVE take-profit: if the TP limit order is moved
# manually at the broker, be_monitor_loop re-reads it every TP_RECHECK_SECS and
# re-bases the 50% trigger to the new target distance (entry → current TP). This
# single open-orders snapshot ALSO serves SL-order-id recovery, so the whole BE
# loop makes at most one REST call per interval regardless of how many positions
# are open (see be_monitor_loop). 6s = ~10 calls/min, far under the 200/60s cap;
# the trigger itself still checks live SignalR quotes every 1s, so BE fires within
# a second of the threshold — only detection of a manual TP move lags by ≤6s.
TP_RECHECK_SECS = 6.0

# Per-symbol break-even state.
# sl_order_ids : list of open SL bracket order IDs for this symbol (one per stacked entry)
# be_triggered : True once BE has fired for the current position build; resets on each new entry
# tp_pts       : the actual TP distance (pts) used to compute the 50% trigger — updated on every entry
# mode         : trading mode captured at entry time (so a mid-position mode switch is safe)
be_state: dict = {}   # symbol -> {"sl_order_ids": [], "be_triggered": False, "tp_pts": 0.0, "mode": ""}

# Symbols whose CURRENT position was opened with Structural SL on. These honour the
# close-beyond-candle-1 invalidation exit. The stored "tf" is the entry's chart
# timeframe (e.g. "5m"); a close-based exit only flattens when the invalidation
# fires on that SAME timeframe (a 1m close can't kill a 5m trade). "level" is the
# candle-1 invalidation price (low for a long, high for a short) sent by Pine as
# sl_price — the bot self-monitors candle closes against it so the structural exit
# no longer depends on a separate TradingView "exit" alert firing. "entry_bucket"
# is the candle index at entry, so the entry candle itself can never trigger the
# exit (only a SUBSEQUENT candle closing beyond the level). Cleared on close.
structural_pos: dict = {}   # symbol -> {"tf": str, "level": float|None, "side": int, "entry_bucket": int}

# Order IDs of the bracket (SL + TP) that belong to each symbol's CURRENT position.
# Captured right after every entry from the same broker snapshot used to arm BE.
# Lets us positively identify ANY other open order on that contract as an orphan —
# e.g. a stale SL/TP left over when a position reverses (the new position's bracket
# sits at new prices while the old one lingers). Union across stacked entries; reset
# on a fresh entry; cleared on close/flatten/flip. See cancel_foreign_orders().
current_bracket_ids: dict = {}   # symbol -> set[int]

# ─── Dashboard State ──────────────────────────────────────────────────────────
bot_paused   = False
trading_mode = "regular"
event_log: deque = deque(maxlen=200)
trade_log: deque = deque(maxlen=500)
sse_clients: list = []

# ─── User-defined setting presets ─────────────────────────────────────────────
# A preset is a named snapshot of every user-tunable setting (see
# _settings_snapshot). The user saves the live settings under a name, then can
# re-apply that exact set later. `active_preset` is the name last saved/applied;
# the dashboard shows it as the current preset ONLY while the live settings still
# match that snapshot — change any setting and it falls back to "default" (the
# match check in _active_preset_name does this; no per-setting bookkeeping needed).
user_presets: dict = {}            # name -> settings snapshot (see _settings_snapshot)
active_preset: str | None = None   # last saved/applied preset name (validated by comparison)
PRESET_LIMIT = 30                  # hard cap on saved presets
PRESET_NAME_MAX = 40               # max characters in a preset name

# ─── Instrument / sizing selection ────────────────────────────────────────────
# enabled_symbols = the ticked instruments that are actively traded. A signal only
# executes if its instrument is in this list. (Persisted.)
enabled_symbols = ["MNQ"]   # subset of INSTRUMENTS

# Per-instrument position sizing: contracts PER ENTRY × max stacked entries.
# Each same-direction signal adds one entry (of `qty`) until the instrument's cap.
SIZING = {
    "MNQ": {"qty": 5, "max_entries": 2},   # 5×2 = 10 contracts max
    "MES": {"qty": 5, "max_entries": 2},   # 5×2 = 10 contracts max
    "MGC": {"qty": 3, "max_entries": 2},   # 3×2 = 6 contracts max
    "NQ":  {"qty": 1, "max_entries": 2},   # 1×2 = 2 contracts max
    "ES":  {"qty": 1, "max_entries": 1},   # 1×1 = 1 contract max
    "GC":  {"qty": 1, "max_entries": 3},   # full-size Gold ($100/pt) — conservative
    "CL":  {"qty": 1, "max_entries": 1},   # full-size Crude ($10/tick) — 1, no stacking
}
_DEFAULT_SIZING = {"qty": 2, "max_entries": 4}   # any unlisted instrument defaults to micro sizing

# ─── Account-size risk presets ────────────────────────────────────────────────
# Selecting a prop-firm account size in the dashboard switches ALL per-instrument
# caps to a risk-bounded preset, so a correlated stop-out across every open
# instrument stays within a fraction of the account's max drawdown. Built for
# Topstep-style combines: 50K = $2,000 trailing DD / $3,000 target; 100K = $3,000
# DD / $6,000 target. Risk basis = the catastrophe safety-net stop × $/point:
#   MNQ $80/ct · MES $100/ct · MGC $200/ct · NQ $800 · ES $1,000 · GC $2,000 · CL $1,500.
# Micros are the workhorses; the full-size NQ/ES/GC/CL are capped at 1 (one GC stop
# alone = the entire 50K drawdown) and should generally stay disabled on these
# accounts. "all open at once" worst case: 50K ≈ $640 (32% of DD), 100K ≈ $1,100.
#
# Two independent knobs (same model as SIZING above):
#   • qty         = contracts the FIRST signal opens — the per-position base size
#                   (this is the order payload "size":_qty in place_order). This is
#                   what a single trade executes regardless of the Stacking toggle.
#   • max_entries = how many entries may stack when Stacking is ON. Each extra
#                   same-direction signal adds another `qty`, up to qty×max_entries.
# So the count the user wants per trade lives in `qty`; stacking depth lives in
# `max_entries`. Per-size intent (MICROS only — MNQ/MES/MGC):
#   50k  → 1 contract, NO stacking            (qty 1 × 1 =  1)
#   100k → 2 base, stacks to 2 entries        (qty 2 × 2 =  4)
#   150k → 3 base, stacks to 3 entries        (qty 3 × 3 =  9)
# The full-size index/gold/crude contracts (NQ/ES/GC/CL) are HARD-CAPPED at 1 contract on
# every account size, stacking or not (qty 1 × 1 = 1) — one of their safety-net
# stops alone is a large slice of the drawdown, so they never scale or stack.
ACCOUNT_SIZE_PRESETS = {
    "50k": {                                    # micros 1, no stacking
        "MNQ": {"qty": 1, "max_entries": 1},
        "MES": {"qty": 1, "max_entries": 1},
        "MGC": {"qty": 1, "max_entries": 1},
        "NQ":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "ES":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "GC":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "CL":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
    },
    "100k": {                                   # micros 2/entry, stack to 4 (2×2)
        "MNQ": {"qty": 2, "max_entries": 2},
        "MES": {"qty": 2, "max_entries": 2},
        "MGC": {"qty": 2, "max_entries": 2},
        "NQ":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "ES":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "GC":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "CL":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
    },
    "150k": {                                   # micros 3/entry, stack to 9 (3×3)
        "MNQ": {"qty": 3, "max_entries": 3},
        "MES": {"qty": 3, "max_entries": 3},
        "MGC": {"qty": 3, "max_entries": 3},
        "NQ":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "ES":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "GC":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
        "CL":  {"qty": 1, "max_entries": 1},    # full-size: hard cap 1
    },
}
ACCOUNT_SIZE_META = {
    "custom": {"label": "Custom",      "target": None, "drawdown": None},
    "50k":    {"label": "50K Combine",  "target": 3000, "drawdown": 2000},
    "100k":   {"label": "100K Combine", "target": 6000, "drawdown": 3000},
    "150k":   {"label": "150K Combine", "target": 9000, "drawdown": 4500},
}
# Active account-size key: "custom" uses the hand-tuned SIZING above; "50k"/"100k"
# apply the risk preset to every NEW position. Persisted.
account_size = "custom"


# ─── Adaptive position-sizing mode (micros only) ──────────────────────────────
# A self-contained, OPT-IN sizing mode. When OFF (the default) NOTHING below runs
# and the bot behaves exactly as before. When ON, for the adaptive micros it:
#   • sizes each entry from a persisted risk multiplier (win ×g / 2nd-loss ×c),
#   • uses a FIXED per-instrument stop/target (plain bracket, no structural exit),
#   • enforces ONE open position account-wide (ignore signals until flat),
#   • counts break-even (|net|≤band) as nothing, carries the multiplier across days,
#   • hard-stops the bot after N losing trades in a day (manual resume only).
# The ladder engine itself lives in adaptive_sizing.py (pure + unit-tested).
adaptive_enabled  = False                       # master toggle (persisted)
adaptive_settings = adaptive.default_settings() # tunable params + per-micro geometry (persisted)
adaptive_state    = adaptive.default_state()    # live ladder: mult / streak / daily stop (persisted)
ADAPTIVE_MICROS   = adaptive.MICROS             # ("MNQ","MES","MGC")


def _futures_day_key() -> str:
    """Label for the current futures trading day (rolls at 18:00 New York), used to
    reset the adaptive daily-loss counter once per day."""
    n = datetime.now(NY_TZ)
    d = n.date()
    if n.hour >= 18:            # after the 6pm NY roll → count toward the next day
        d = d + timedelta(days=1)
    return d.isoformat()


def adaptive_active_for(symbol: str) -> bool:
    """True only when adaptive mode is ON and this symbol is an adaptive micro."""
    return bool(adaptive_enabled) and symbol in ADAPTIVE_MICROS


def adaptive_qty(symbol: str) -> int:
    """Micros to trade on the next adaptive entry (multiplier × base, floor 1, no cap)."""
    return adaptive.contracts_for(adaptive_state.get("mult", 1.0),
                                  adaptive_settings.get("base_micros", 1))


def _any_position_open() -> bool:
    """True if ANY enabled instrument currently holds a position (account-wide lock)."""
    for s in enabled_symbols:
        if (positions.get(s) or {}).get("direction", "FLAT") != "FLAT":
            return True
    return False


def active_sizing() -> dict:
    """The sizing table currently in force — a risk preset when an account size is
    selected, otherwise the hand-tuned SIZING."""
    return ACCOUNT_SIZE_PRESETS.get(account_size, SIZING)


def qty_for(symbol: str) -> int:
    return active_sizing().get(symbol, _DEFAULT_SIZING)["qty"]


def max_contracts_for(symbol: str) -> int:
    s = active_sizing().get(symbol, _DEFAULT_SIZING)
    return s["qty"] * s["max_entries"]


def sizing_summary() -> str:
    """Compact human string, e.g. 'MNQ/MES/MGC 2×4=8 · NQ 1×3=3'."""
    tbl = active_sizing()
    groups: dict = {}
    for sym in INSTRUMENTS:
        s   = tbl.get(sym, _DEFAULT_SIZING)
        key = (s["qty"], s["max_entries"])
        groups.setdefault(key, []).append(sym)
    body = " · ".join(
        f"{'/'.join(syms)} {q}×{m}={q*m}" for (q, m), syms in groups.items()
    )
    lbl = ACCOUNT_SIZE_META.get(account_size, {}).get("label")
    return f"[{lbl}] {body}" if account_size in ACCOUNT_SIZE_PRESETS else body


def _sizing_table_for(key: str) -> dict:
    """The sizing table for an account-size key ('custom'→SIZING, else its preset)."""
    return ACCOUNT_SIZE_PRESETS.get(key, SIZING)


def account_size_breakdown() -> dict:
    """Per-account-size, per-instrument max contracts and worst-case $ risk (max
    contracts × safety-net pts × $/point), for the dashboard's (i) sizing tooltip."""
    out: dict = {}
    for key in ACCOUNT_SIZE_META:
        tbl  = _sizing_table_for(key)
        rows = []
        for sym in INSTRUMENTS:
            s   = tbl.get(sym, _DEFAULT_SIZING)
            mx  = s["qty"] * s["max_entries"]
            pv  = (contracts.get(sym) or {}).get("point_value") or POINT_VALUES.get(sym, 1.0)
            rows.append({
                "sym":  sym,
                "max":  mx,
                "risk": round(mx * SAFETY_NET_PTS.get(sym, 40) * pv),
            })
        out[key] = rows
    return out


# ─── Per-instrument direction bias ────────────────────────────────────────────
# Each instrument can be locked to one side so it only trades with its higher-
# timeframe trend: "both" (default) takes long+short, "long" blocks short signals,
# "short" blocks long signals. e.g. set MGC "short" in a bearish gold regime and
# every bullish-IFVG buy on MGC is dropped at the webhook. Persisted. A symbol
# absent from the dict means "both".
DIRECTION_OPTIONS = ("both", "long", "short")
DIRECTION_LABELS  = {"both": "Both", "long": "Long-only", "short": "Short-only"}
instrument_bias: dict = {}   # symbol -> "long" | "short"  (missing = "both")


def bias_for(symbol: str) -> str:
    return instrument_bias.get(symbol, "both")


def bias_allows(symbol: str, side: int) -> bool:
    """side 0 = buy/long, side 1 = sell/short. True if this instrument's bias
    permits opening in that direction."""
    b = bias_for(symbol)
    if b == "long":  return side == 0
    if b == "short": return side == 1
    return True


def blank_pos() -> dict:
    return {"size": 0, "side": None, "direction": "FLAT", "avg_price": None}


def blank_contract() -> dict:
    return {"id": None, "name": None, "symbol_id": None,
            "tick": 0.25, "point_value": 1.0, "description": None, "_ts": 0.0}


def blank_quote() -> dict:
    return {"last": None, "bid": None, "ask": None,
            "change": None, "change_pct": None, "high": None, "low": None}


# Per-instrument state, keyed by ticker. Populated for every enabled symbol.
contracts: dict = {}    # symbol -> resolved contract (id/tick/point_value/…)
positions: dict = {}    # symbol -> position (size/side/direction/avg_price)
quotes:    dict = {}    # symbol -> live quote


# Reverse map contractId -> ticker, rebuilt whenever contracts[] changes.
# symbol_for_contract() sits on the per-tick quote path (the highest-frequency
# event in the process), so it must be an O(1) lookup, not a scan.
contract_to_symbol: dict = {}


def _rebuild_contract_map():
    contract_to_symbol.clear()
    contract_to_symbol.update(
        {c["id"]: s for s, c in contracts.items() if c.get("id")}
    )


def symbol_for_contract(cid):
    """Reverse-map a broker contractId back to its ticker (for routing
    SignalR position/trade/quote events to the right instrument)."""
    return contract_to_symbol.get(cid) if cid else None


# Serializes order execution. TradingView fires several timeframes (and now
# several instruments) at once, so multiple signals can land within the same
# second; without this lock their execute_trade() coroutines interleave, read
# each other's stale position, and leave the account in a wrong net state.
trade_lock = asyncio.Lock()

# ─── SignalR Live State ───────────────────────────────────────────────────────
signalr_user_connected   = False
signalr_market_connected = False
_market_ws               = None   # handle to the live market socket (for resubscribe on instrument change)
_user_ws                 = None   # handle to the live user socket (for resubscribe on account change)
account_balance: float   = None
account_can_trade: bool  = True

session_stats = {
    "signals_received": 0,
    "buys":             0,
    "sells":            0,
    "orders_placed":    0,
    "orders_failed":    0,
    "reversals":        0,
    "realized_pnl":     0.0,   # running net P&L of closed trades this session
    "wins":             0,
    "losses":           0,
    "start_time":       datetime.now(IST).strftime("%I:%M %p") + " IST",
}

# UTC instant this session began — the lower bound for "this session's" closed
# trades when realized P&L / win-loss is reconciled from the broker. Resets on
# every (re)start, exactly like session_stats above.
_SESSION_START_UTC = datetime.now(timezone.utc)


# ─── Circuit breaker ──────────────────────────────────────────────────────────
# After N consecutive LOSING closed trades — counted across ALL instruments, in
# the order the broker actually closed them — auto-PAUSE every instrument for a
# cooldown, then resume automatically. A single win anywhere resets the streak.
# This is the prop-firm safety rail KAIROS was missing: one bad run can fail a
# combine, and nothing here used to stop it. Test signals are unaffected (they
# already bypass the pause gate).
CIRCUIT_BREAKER_LOSSES   = 3      # consecutive losses that trip the breaker
CIRCUIT_BREAKER_COOLDOWN = 600    # seconds paused before auto-resume (10 min)
# NOTE: the breaker ALWAYS auto-resumes after the cooldown. The old "daily hard
# halt" (which left the bot paused indefinitely until a manual resume) was removed
# by design — the bot must never stop trading without the user pressing Pause.

# Runtime state (not persisted — the bot always boots un-paused, like bot_paused).
#   active          — True while a breaker cooldown is holding the pause
#   until           — wall-clock deadline of the current cooldown (0 when inactive)
#   handled_through — number of closed trades already consumed by a prior trip, so
#                     the SAME losing run can never re-trip after auto-resume.
#   trips           — circuit-breaker trips so far this session (informational)
_breaker = {"active": False, "until": 0.0, "handled_through": 0, "trips": 0}


# ─── State Persistence ────────────────────────────────────────────────────────
# save_state() used to json.dump ~100KB synchronously on EVERY order/fill —
# blocking the event loop that also services the SignalR readers and webhook.
# It now just marks state dirty; a background task flushes within ~2s via a
# worker thread, writing tmp-file + os.replace so a crash mid-write can never
# corrupt bot_state.json. Trade-off: up to ~2s of unflushed state on a hard kill.
_state_dirty = False


def _serialize_state() -> dict:
    return {
        "trading_mode":    trading_mode,
        "trade_settings":  trade_settings,
        "enabled_symbols": enabled_symbols,
        "instrument_bias": instrument_bias,
        "active_account":  active_account,
        "account_size":    account_size,
        "min_fvg_ticks":   min_fvg_ticks,
        "max_stop_ticks":  max_stop_ticks,
        "tf1m_session":    tf1m_session,
        "macro_window":    macro_window,
        "user_presets":    user_presets,
        "active_preset":   active_preset,
        # Adaptive sizing mode (opt-in). The ladder state is persisted on purpose so
        # the multiplier CARRIES across restarts and days (unlike the breaker).
        "adaptive_enabled":  adaptive_enabled,
        "adaptive_settings": adaptive_settings,
        "adaptive_state":    adaptive_state,
        # Strip separator entries before saving — they're re-added on load
        "trade_log": [t for t in trade_log if not t.get("separator")],
    }


# ─── Setting presets ──────────────────────────────────────────────────────────
# The "settings" a preset captures: every user-tunable strategy/risk knob. It
# deliberately EXCLUDES operational state (open positions, pause/circuit-breaker,
# trade log) and the broker account identity (`active_account`) — switching the
# broker account has its own guard rails and is not a tuning choice.
def _settings_snapshot() -> dict:
    """A normalized snapshot of the live user-tunable settings. Normalized so two
    snapshots compare equal with `==` iff every setting matches (enabled_symbols
    is order-insensitive, nested dicts are copied)."""
    return {
        "trading_mode":     trading_mode,
        "trade_settings":   dict(trade_settings),
        "enabled_symbols":  sorted(enabled_symbols),
        "instrument_bias":  dict(instrument_bias),
        "account_size":     account_size,
        "min_fvg_ticks":    dict(min_fvg_ticks),
        "max_stop_ticks":   dict(max_stop_ticks),
        "tf1m_session":     tf1m_session,
        "macro_window":     macro_window,
    }


def _apply_settings_snapshot(ps: dict):
    """Load a saved preset's settings into the live globals, validating every
    field exactly as load_state() does. Touches ONLY settings — positions, the
    broker account, and pause state are left untouched."""
    global trading_mode, trade_settings, enabled_symbols, account_size, macro_window, tf1m_session
    # Trading mode.
    tm = ps.get("trading_mode")
    if tm in MODES:
        trading_mode = tm
    # Trade settings: start from the mode's preset, overlay the saved values,
    # then validate (same rules as load_state).
    base = dict(MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"]))
    saved_ts = ps.get("trade_settings")
    if isinstance(saved_ts, dict):
        for k in ("tp1", "tp2", "structural_sl", "auto_be", "macro_time", "structural_tp", "swing_stop", "aplus_enabled", "aplus_tp_nasdaq", "aplus_tp_spgold", "aplus_max_session"):
            if k in saved_ts:
                base[k] = saved_ts[k]
    if base.get("tp1") not in TP1_OPTIONS:
        base["tp1"] = MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"])["tp1"]
    if base.get("tp2") not in TP2_OPTIONS:
        base["tp2"] = MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"])["tp2"]
    base["structural_sl"] = bool(base["structural_sl"])
    base["auto_be"]       = bool(base["auto_be"])
    base["macro_time"]    = bool(base.get("macro_time", False))
    base["swing_stop"]    = bool(base.get("swing_stop", True))
    base.pop("hybrid_stop", None)   # legacy mode removed
    _coerce_aplus(base)
    if base.get("structural_tp") not in STRUCT_TP_OPTIONS:
        base["structural_tp"] = "3x"
    _enforce_stop_mode(base)   # exactly one of structural / swing
    trade_settings = base
    # Enabled instruments (keep current set if the preset has none valid).
    raw = ps.get("enabled_symbols")
    if isinstance(raw, list):
        cleaned = [s for s in raw if s in INSTRUMENTS]
        if cleaned:
            enabled_symbols = cleaned
    # Per-instrument direction bias (replace wholesale; missing = "both").
    bias = ps.get("instrument_bias")
    if isinstance(bias, dict):
        instrument_bias.clear()
        for s, v in bias.items():
            if s in INSTRUMENTS and v in ("long", "short"):
                instrument_bias[s] = v
    # Account-size risk preset.
    asz = ps.get("account_size")
    if asz in ACCOUNT_SIZE_META:
        account_size = asz
    # Per-group Min FVG / Max Stop (mutated in place).
    mft = ps.get("min_fvg_ticks")
    if isinstance(mft, dict):
        for g in MIN_FVG_GROUPS:
            v = mft.get(g)
            if isinstance(v, (int, float)) and 0 <= v <= 1000:
                min_fvg_ticks[g] = int(v)
    mst = ps.get("max_stop_ticks")
    if isinstance(mst, dict):
        for g in MIN_FVG_GROUPS:
            v = mst.get(g)
            if isinstance(v, (int, float)) and 0 <= v <= 10000:
                max_stop_ticks[g] = int(v)
    # (Re-entry cooldown removed — presets saved before the removal may still
    # carry a "reentry_cooldown" key; it is simply ignored.)
    # 1-minute session mode.
    _s1 = str(ps.get("tf1m_session", "")).upper()
    if _s1 in TF1M_SESSION_OPTIONS:
        tf1m_session = _s1
    # Macro window width.
    try:
        if int(ps.get("macro_window")) in MACRO_WINDOW_OPTIONS:
            macro_window = int(ps["macro_window"])
    except (ValueError, TypeError):
        pass


def _active_preset_name():
    """The preset the dashboard should show as 'current': the last saved/applied
    one, but ONLY while the live settings still match its snapshot. Any setting
    change makes the snapshot diverge, so this returns None ("default") — exactly
    the 'changing a setting drops the preset' behaviour, with no per-setting hooks."""
    if active_preset and active_preset in user_presets \
            and user_presets[active_preset] == _settings_snapshot():
        return active_preset
    return None


def _preset_desc(ps: dict) -> str:
    """A full one-line summary of a preset snapshot — EVERY setting it carries
    (mode, trade settings, instruments, bias, account size, FVG/stop, cooldown,
    timeframe), so the activity log shows exactly what saving/applying covers."""
    ts   = ps.get("trade_settings", {}) or {}
    inst = ",".join(ps.get("enabled_symbols", []) or []) or "none"
    bias = ps.get("instrument_bias", {}) or {}
    bias_txt = ", ".join(f"{s} {v}" for s, v in sorted(bias.items())) or "all both"
    mfv = ps.get("min_fvg_ticks", {}) or {}
    mst = ps.get("max_stop_ticks", {}) or {}
    fvg_txt = " ".join(f"{g} {v}t" for g, v in mfv.items()) or "—"
    max_txt = " ".join(f"{g} {v}t" for g, v in mst.items()) or "—"
    return (
        f"Stop {_stop_mode_label(ts)} · TP {ts.get('structural_tp')}×stop · "
        f"BE {'on' if ts.get('auto_be') else 'off'} · "
        f"Macro {'on' if ts.get('macro_time') else 'off'} · "
        f"1m {ps.get('tf1m_session', 'ETH')} · "
        f"Acct {ps.get('account_size')} · "
        f"Inst {inst} · Bias {bias_txt} · "
        f"MinFVG {fvg_txt} · MaxStop {max_txt}")


def _write_state_file(data: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, STATE_FILE)   # atomic on POSIX


def save_state():
    """Mark state dirty for the debounced writer. Falls back to an immediate
    synchronous write when no event loop is running (startup/shutdown paths)."""
    global _state_dirty
    _state_dirty = True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            _write_state_file(_serialize_state())
            _state_dirty = False
        except Exception as e:
            print(f"[STATE] Save failed: {e}")


async def state_writer_loop():
    """Flush dirty state to disk every 2s, off the event loop."""
    global _state_dirty
    while True:
        await asyncio.sleep(2)
        if not _state_dirty:
            continue
        _state_dirty = False
        try:
            data = _serialize_state()          # cheap dict build on the loop
            await asyncio.to_thread(_write_state_file, data)   # I/O in a thread
        except Exception as e:
            print(f"[STATE] Save failed: {e}")
            _state_dirty = True                # retry on the next tick


async def heartbeat_loop():
    """Top of every hour, push a slim 'still online' heartbeat so an outage on the
    home machine doesn't go unnoticed. Plain one-liner (no embed) to stay quiet."""
    while True:
        now = datetime.now(NY_TZ)
        await asyncio.sleep(max(30, 3600 - (now.minute * 60 + now.second)))
        asyncio.create_task(notify_push(
            f"💚 KAIROS online · {now_ny()} NY ({now_ist('%I:%M %p')} IST)"))


def load_state():
    """Load persisted state on startup. Adds a session separator to trade_log."""
    global trading_mode, trade_log, enabled_symbols, trade_settings, active_account
    global instrument_bias, account_size, user_presets, active_preset, macro_window, tf1m_session
    global adaptive_enabled, adaptive_settings, adaptive_state
    if not os.path.exists(STATE_FILE):
        print("[STATE] No state file found — starting fresh")
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)

        trading_mode  = data.get("trading_mode", "bracket")
        if trading_mode == "gold_scalp":      # legacy key → renamed to "5pt Scalp"
            trading_mode = "scalp_5pt"
        if trading_mode == "manual":          # legacy key → renamed to "Bracket"
            trading_mode = "bracket"
        # Removed modes (volatile/regular/conservative/structural) → default Bracket.
        if trading_mode not in MODES:
            trading_mode = "bracket"
        # Trade settings: persisted override if present, else the mode's preset.
        _preset = dict(MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"]))
        _saved  = data.get("trade_settings")
        if isinstance(_saved, dict):
            for k in ("tp1", "tp2", "structural_sl", "auto_be", "macro_time", "structural_tp", "swing_stop", "aplus_enabled", "aplus_tp_nasdaq", "aplus_tp_spgold", "aplus_max_session"):
                if k in _saved:
                    _preset[k] = _saved[k]
            if _preset.get("tp1") not in TP1_OPTIONS:
                _preset["tp1"] = MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"])["tp1"]
            if _preset.get("tp2") not in TP2_OPTIONS:
                _preset["tp2"] = MODE_PRESETS.get(trading_mode, MODE_PRESETS["bracket"])["tp2"]
            _preset["structural_sl"] = bool(_preset["structural_sl"])
            _preset["auto_be"]       = bool(_preset["auto_be"])
            _preset["macro_time"]    = bool(_preset.get("macro_time", False))
            _preset["swing_stop"]    = bool(_preset.get("swing_stop", True))
            _preset.pop("hybrid_stop", None)   # legacy mode removed
            _coerce_aplus(_preset)
            if _preset.get("structural_tp") not in STRUCT_TP_OPTIONS:
                _preset["structural_tp"] = "3x"
            _enforce_stop_mode(_preset)   # exactly one of structural / swing
        trade_settings = _preset
        # Accept the new list form; fall back to the old single-symbol field.
        raw = data.get("enabled_symbols")
        if raw is None:
            old = data.get("selected_symbol", "MNQ")
            raw = [old]
        cleaned = [s for s in raw if s in INSTRUMENTS]
        enabled_symbols = cleaned or ["MNQ"]
        # Per-instrument direction bias (only keep valid, non-default entries).
        _bias = data.get("instrument_bias")
        if isinstance(_bias, dict):
            instrument_bias = {
                s: v for s, v in _bias.items()
                if s in INSTRUMENTS and v in ("long", "short")
            }
        # Persisted account selection takes precedence over the .env default.
        _acct = data.get("active_account")
        if _acct:
            active_account = _acct
        # Account-size risk preset (custom / 50k / 100k).
        _asz = data.get("account_size")
        if _asz in ACCOUNT_SIZE_META:
            account_size = _asz
        # Per-group Minimum FVG Ticks (mutated in place; keeps defaults if absent).
        _mft = data.get("min_fvg_ticks")
        if isinstance(_mft, dict):
            for _g in MIN_FVG_GROUPS:
                _v = _mft.get(_g)
                if isinstance(_v, (int, float)) and 0 <= _v <= 1000:
                    min_fvg_ticks[_g] = int(_v)
        # Per-group max stop-distance cap (ticks).
        _mst = data.get("max_stop_ticks")
        if isinstance(_mst, dict):
            for _g in MIN_FVG_GROUPS:
                _v = _mst.get(_g)
                if isinstance(_v, (int, float)) and 0 <= _v <= 10000:
                    max_stop_ticks[_g] = int(_v)
        # (Re-entry cooldown removed — an old "reentry_cooldown" key in
        # bot_state.json is simply ignored.)
        # 1-minute session mode.
        _s1 = str(data.get("tf1m_session", "")).upper()
        if _s1 in TF1M_SESSION_OPTIONS:
            tf1m_session = _s1
        # Macro window width.
        try:
            if int(data.get("macro_window")) in MACRO_WINDOW_OPTIONS:
                macro_window = int(data["macro_window"])
        except (ValueError, TypeError):
            pass
        # Adaptive sizing mode (opt-in). Validate settings; keep the persisted
        # ladder state (mult/streak/daily-stop) so it carries across restarts/days.
        adaptive_enabled = bool(data.get("adaptive_enabled", False))
        adaptive_settings = adaptive.sanitize_settings(data.get("adaptive_settings"))
        _as = data.get("adaptive_state")
        if isinstance(_as, dict):
            _st = adaptive.default_state()
            _st.update({k: _as[k] for k in _st if k in _as})
            adaptive_state = _st
        # User-defined setting presets + the active selection.
        _up = data.get("user_presets")
        if isinstance(_up, dict):
            user_presets = {str(k): v for k, v in _up.items() if isinstance(v, dict)}
        _ap = data.get("active_preset")
        if isinstance(_ap, str) and _ap in user_presets:
            active_preset = _ap
            # Re-arm the last-used preset on restart. The individual settings above
            # were already restored from their own keys, but we additionally APPLY
            # the active preset's snapshot so the preset the user last saved/applied
            # is exactly what's loaded — and then re-store it in the CURRENT snapshot
            # schema. The re-store migrates an old-schema preset (e.g. the retired
            # `timeframe_rules` field) so it still equals _settings_snapshot() and the
            # dashboard shows it as armed instead of silently falling back to "Default".
            try:
                _apply_settings_snapshot(user_presets[active_preset])
                user_presets[active_preset] = _settings_snapshot()
            except Exception as _e:
                print(f"[STATE] Could not re-arm preset '{active_preset}': {_e}")
        saved_trades  = data.get("trade_log", [])
        trade_log     = deque(saved_trades, maxlen=500)

        # Prepend session separator so the trade log shows a clear break
        separator = {
            "separator": True,
            "time_ist":  datetime.now(IST).strftime("%d %b %Y · %I:%M %p") + " IST",
            "label":     "─── New Session ───",
        }
        trade_log.appendleft(separator)

        print(f"[STATE] Loaded — {len(saved_trades)} trade(s), mode: {trading_mode}")
    except Exception as e:
        print(f"[STATE] Load failed: {e} — starting fresh")


# Run once on startup
load_state()


# ─── Helpers ─────────────────────────────────────────────────────────────────
def now_ist(fmt="%I:%M:%S %p"):
    return datetime.now(IST).strftime(fmt)


def now_ny(fmt="%I:%M %p"):
    return datetime.now(NY_TZ).strftime(fmt)


# ─── Trade-results log (results.txt) ──────────────────────────────────────────
# Every CLOSED trade is appended here with its entry time + classified outcome,
# grouped under a divider per trading session. A session rolls at 6:00 PM New York
# (the daily futures open), so the file reads as one block per trading day for
# offline analysis by instrument / time window / stop mode / outcome.
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.txt")
_results_last_session = None   # session id ("YYYY-MM-DD") of the last divider written


def _ny_session_open(dt=None):
    """The 6 PM-NY open datetime of the trading session that contains `dt` (now if
    None). Trades before 6 PM NY belong to the session that opened the prior 6 PM."""
    n = (dt or datetime.now(timezone.utc)).astimezone(NY_TZ)
    base = n.replace(hour=18, minute=0, second=0, microsecond=0)
    if n.hour < 18:
        base -= timedelta(days=1)
    return base


def _ny_session_id(dt=None):
    return _ny_session_open(dt).strftime("%Y-%m-%d")


def _ny_session_label(sess_id):
    try:
        return datetime.strptime(sess_id, "%Y-%m-%d").strftime("%a %d %b %Y") + " · 6:00 PM NY open"
    except Exception:
        return sess_id


def _init_results_session():
    """Seed _results_last_session from the file so a restart mid-session doesn't
    write a duplicate divider."""
    global _results_last_session
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE) as f:
                for line in f:
                    if line.startswith("SESSION "):
                        _results_last_session = line.split("SESSION ", 1)[1].strip().split()[0]
    except Exception:
        pass


def _result_line_text(e) -> str:
    _ap = " [A+]" if e.get("a_plus") else ""
    return (f"[entry {e.get('time_ny','—')} NY -> exit {e.get('exit_ny','—')} NY] "
            f"{str(e.get('instrument','—')):<4} {str(e.get('direction','—')):<4} "
            f"x{e.get('size','?'):<2} {str(e.get('stop_mode','—')):<10} -> "
            f"{str(e.get('outcome','—')):<26} {str(e.get('pnl','—')):>11}  "
            f"(SL {e.get('sl_pts','?')}pt / TP {e.get('tp_pts','?')}pt"
            f"{' @ exit ' + str(e.get('exit_price')) if e.get('exit_price') is not None else ''}"
            f"{' · ' + str(e.get('ifvg_source')) if e.get('ifvg_source') not in (None,'—') else ''}{_ap})")


def _append_result_line(e):
    """Append one finalized trade to results.txt under the right session divider."""
    global _results_last_session
    sess = e.get("session") or _ny_session_id()
    try:
        with open(RESULTS_FILE, "a") as f:
            if sess != _results_last_session:
                _results_last_session = sess
                f.write(f"\n{'='*72}\nSESSION {sess}  ({_ny_session_label(sess)})\n{'='*72}\n")
            f.write(_result_line_text(e) + "\n")
    except Exception as ex:
        print(f"[RESULTS] write failed: {ex}")


def _stamp_open_rows(symbol: str, **fields):
    """Set fields on every still-open (pnl == '—') trade_log row for a symbol —
    used to record live exit hints (be_fired / reached_phase2 / exit_reason) before
    the close P&L lands and the outcome is classified."""
    for e in trade_log:
        if (not e.get("separator") and e.get("instrument") == symbol
                and e.get("pnl") == "—"):
            e.update(fields)


def _parse_net(s) -> float:
    """Parse a '+$12.00' / '-$5.00' net-P&L string to a float."""
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace("$", "").replace("+", "").replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _classify_outcome(e, net: float) -> str:
    """Classify how a trade closed, from the live exit hints stamped on its row plus
    the realized net P&L. Every closed trade gets exactly one outcome label."""
    r = (e.get("exit_reason") or "").lower()
    if "break-even" in r or "break even" in r:
        return "Break-even (BE hit)"
    if "structural" in r or "close beyond" in r or "invalidat" in r:
        return "Close-based exit"
    if r:   # any other explicit flatten (manual / reversal / hedge)
        return "Manual / reversal close"
    # No explicit reason → a broker bracket fill (TP or SL).
    if e.get("be_fired"):
        return "TP hit" if net > 0.01 else "Break-even (BE hit)"
    if net > 0:
        return "TP hit"
    # A loss with no BE: candle-1 stop if it never survived the entry candle, else swing.
    if e.get("reached_phase2"):
        return "Stop · swing" if e.get("stop_mode") in ("Hybrid", "Swing") else "Stop · structural"
    return "Stop · candle-1 (immediate)"


def _finalize_trade_result(e, net_str, notify: bool = True) -> bool:
    """Set pnl + classified outcome on a trade row (once) and append it to
    results.txt. Returns True if it newly finalized the row (for save_state).
    `notify=False` suppresses the A+ result push — used when back-filling rows from
    PAST sessions on startup, so old closes never re-alert Discord/Telegram."""
    if e.get("result_logged"):
        return False
    # Capture the EXIT: NY time now (close is being processed) + a price proxy from
    # the live quote at close.
    e["exit_ny"]    = now_ny()
    _xq = (quotes.get(e.get("instrument")) or {}).get("last")
    e["exit_price"] = round(float(_xq), 4) if isinstance(_xq, (int, float)) else None
    e["pnl"]           = net_str
    e["outcome"]       = _classify_outcome(e, _parse_net(net_str))
    e["result_logged"] = True
    _append_result_line(e)
    # A+ trades → push the result (win/loss/BE) with P&L. A+-only to keep it signal.
    if e.get("a_plus") and notify:
        _net = _parse_net(net_str)
        _oc  = e.get("outcome", "—")
        _ck  = "be" if "break-even" in _oc.lower() else ("win" if _net > 0 else "loss")
        _icon = {"win": "✅", "loss": "❌", "be": "⚖️"}[_ck]
        _fire_push(
            f"{_icon} A+ {_oc} · {e.get('instrument')} {e.get('direction')} · {net_str}",
            mention=True,
            embed=_embed(f"{_icon} A+ {_oc} — {e.get('instrument')} {e.get('direction')}", _ck,
                         fields=[("Result", _oc), ("P&L", net_str),
                                 ("Entry → Exit", f"{e.get('time_ny','—')} → {e.get('exit_ny','—')} NY")],
                         footer=f"SL {e.get('sl_pts','?')}pt · TP {e.get('tp_pts','?')}pt"))
    return True


# Persistent rotating handler for trade_logs.txt — replaces the old open/close
# per log line, and caps growth at 10MB × 3 backups.
# GUARD: `uvicorn.run("main:app")` imports this module twice (once as __main__
# from the bottom block, once as `main`). The logger is a process-wide singleton
# keyed by name, so an unguarded addHandler() would attach TWICE and write every
# line twice. Only add the handler if this logger has none yet.
_file_logger = logging.getLogger("kairos.tradefile")
_file_logger.setLevel(logging.INFO)
_file_logger.propagate = False
if not _file_logger.handlers:
    _fh = RotatingFileHandler("trade_logs.txt", maxBytes=10_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(message)s"))
    _file_logger.addHandler(_fh)


def log(message: str, event_type: str = "system", ifvg_info: str = None, feed_msg: str = None) -> dict:
    # The full `message` always goes to the console + trade_logs.txt (for debugging).
    # The activity feed shows `feed_msg` when given (a short version), else `message`.
    ts_file = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    print(f"[{ts_file}] {message}")
    _file_logger.info(f"[{ts_file}] {message}")
    entry = {
        "timestamp": now_ist(),
        "type":      event_type,
        "message":   feed_msg if feed_msg is not None else message,
        "ifvg_info": ifvg_info,
    }
    event_log.appendleft(entry)
    return entry


async def alog(message: str, event_type: str = "system", ifvg_info: str = None, feed_msg: str = None):
    entry = log(message, event_type, ifvg_info, feed_msg)
    for q in sse_clients[:]:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            try: sse_clients.remove(q)
            except ValueError: pass


def unrealized_pnl(symbol):
    """Unrealized P&L (dollars) for one instrument: live price vs avg entry."""
    pos = positions.get(symbol)
    if not pos or pos["direction"] == "FLAT":
        return None
    q   = quotes.get(symbol) or {}
    avg = pos.get("avg_price")
    last = q.get("last")
    if last is None or avg is None:
        return None
    diff = (last - avg) if pos["direction"] == "LONG" else (avg - last)
    pv   = (contracts.get(symbol, {}) or {}).get("point_value") or 1.0
    return round(diff * pos["size"] * pv, 2)


def total_unrealized_pnl():
    vals = [unrealized_pnl(s) for s in enabled_symbols]
    vals = [v for v in vals if v is not None]
    return round(sum(vals), 2) if vals else None


async def push_state_update():
    """Lightweight SSE nudge — makes every dashboard client call refreshState()."""
    entry = {"type": "state_update", "timestamp": now_ist()}
    for q in sse_clients[:]:
        try:
            q.put_nowait(entry)
        except (asyncio.QueueFull, ValueError):
            try: sse_clients.remove(q)
            except ValueError: pass



# ─── TopstepX API ─────────────────────────────────────────────────────────────
async def get_token_and_account(client):
    auth_res = await client.post(
        "https://api.topstepx.com/api/Auth/loginKey",
        json={"userName": USERNAME, "apiKey": API_KEY},
    )
    if auth_res.status_code != 200:
        await alog(f"Auth failed: {auth_res.status_code} — {auth_res.text}", "error")
        return None, None
    token = auth_res.json().get("token")
    if not token:
        await alog(f"No token in response", "error")
        return None, None

    acc_res = await client.post(
        "https://api.topstepx.com/api/Account/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"onlyActiveAccounts": True},
    )
    if acc_res.status_code != 200:
        await alog(f"Account search failed: {acc_res.status_code}", "error")
        return None, None

    global available_accounts
    acc_data   = acc_res.json()
    accounts   = acc_data.get("accounts", []) if isinstance(acc_data, dict) else acc_data
    found      = [a.get("name") for a in accounts if isinstance(a, dict) and a.get("name")]
    available_accounts = found            # keep the dashboard dropdown fresh
    account_id = None
    for acc in accounts:
        if isinstance(acc, dict) and acc.get("name") == active_account:
            account_id = acc.get("id")
            break

    if not account_id:
        await alog(f"Account '{active_account}' not found. Available: {found}", "error")
        return None, None
    return token, account_id


async def resolve_contract(client, token, ticker: str):
    """Look up the live, front-month contract for a ticker via Contract/search.
    The contract `id` uses an internal symbol (NQ → CON.F.US.ENQ...), but the
    `name` field uses the ticker (e.g. 'NQU5'), so we match on name prefix +
    month code to pick the right one and never confuse e.g. NQ with MNQ."""
    try:
        res = await client.post(
            "https://api.topstepx.com/api/Contract/search",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"live": False, "searchText": ticker},
        )
        if res.status_code != 200:
            await alog(f"Contract search failed for {ticker}: {res.status_code}", "warning")
            return None
        found = res.json().get("contracts", []) or []
    except Exception as e:
        await alog(f"Contract search error for {ticker}: {e}", "error")
        return None

    def is_match(c):
        n = c.get("name") or ""
        return (n[:len(ticker)] == ticker and len(n) > len(ticker)
                and n[len(ticker)] in MONTH_CODES)

    matches = [c for c in found if is_match(c)]
    active  = [c for c in matches if c.get("activeContract")]
    chosen  = active or matches
    if not chosen:
        await alog(f"No active contract found for '{ticker}'", "warning")
        return None

    c     = chosen[0]
    tick  = c.get("tickSize") or 0.25
    tval  = c.get("tickValue")
    pv    = (tval / tick) if (tval and tick) else None
    return {
        "id":          c.get("id"),
        "name":        c.get("name"),
        "symbol_id":   c.get("symbolId"),
        "tick":        tick,
        "point_value": pv,
        "description": c.get("description"),
    }


async def ensure_contracts(client, token, force: bool = False):
    """Resolve the live contract for every ENABLED instrument into contracts[].
    Re-resolves if forced or if a cached resolution is >6h old (picks up a
    quarterly roll without a restart). Drops contracts no longer enabled."""
    for sym in enabled_symbols:
        c = contracts.get(sym)
        fresh = (c and c.get("id")
                 and (time.monotonic() - c.get("_ts", 0)) < 6 * 3600)
        if fresh and not force:
            continue
        info = await resolve_contract(client, token, sym)
        if info and info.get("id"):
            info["_ts"] = time.monotonic()
            contracts[sym] = info
            positions.setdefault(sym, blank_pos())
            quotes.setdefault(sym, blank_quote())
            await alog(
                f"Contract → {sym}: {info['name']} ({info['id']}) | "
                f"tick {info['tick']} = ${(info['point_value'] or 0):.2f}/pt",
                "system",
            )
    # Forget instruments that are no longer enabled
    for sym in list(contracts.keys()):
        if sym not in enabled_symbols:
            contracts.pop(sym, None)
    _rebuild_contract_map()


async def refresh_positions(client, token, account_id, only_symbol=None):
    """Read all open positions once and update positions[] for every enabled
    instrument. Returns (size, side) for only_symbol when given."""
    try:
        res = await client.post(
            "https://api.topstepx.com/api/Position/searchOpen",
            headers={"Authorization": f"Bearer {token}"},
            json={"accountId": account_id},
        )
        if res.status_code != 200:
            await alog(f"Position check failed: {res.status_code}", "warning")
            return (positions.get(only_symbol, blank_pos())["size"],
                    positions.get(only_symbol, blank_pos())["side"]) if only_symbol else (0, None)

        data    = res.json()
        plist   = data.get("positions", []) if isinstance(data, dict) else data
        by_cid  = {p.get("contractId"): p for p in plist if isinstance(p, dict)}

        for sym in enabled_symbols:
            cid = (contracts.get(sym) or {}).get("id")
            p   = by_cid.get(cid)
            prev_avg = (positions.get(sym) or {}).get("avg_price")
            if p:
                # Position/searchOpen returns type (1=Long, 2=Short) + size (always
                # positive) — there is NO signed `netPos` field. The old netPos
                # lookup therefore always read 0, so every open position looked FLAT
                # and signals never reversed/flattened (they just stacked opposing
                # orders that netted into a hedge).
                ptype = p.get("type", 0)
                size  = p.get("size", 0) or 0
                avg   = p.get("averagePrice", prev_avg)
                if size and ptype == 1:
                    positions[sym] = {"size": size, "side": 0, "direction": "LONG",  "avg_price": avg}
                elif size and ptype == 2:
                    positions[sym] = {"size": size, "side": 1, "direction": "SHORT", "avg_price": avg}
                else:
                    positions[sym] = blank_pos()
            else:
                positions[sym] = blank_pos()

        if only_symbol:
            pp = positions.get(only_symbol, blank_pos())
            return pp["size"], pp["side"]
        return 0, None
    except Exception as e:
        await alog(f"Position check error: {e}", "error")
        return 0, None


async def search_open_orders(client, token, account_id) -> list:
    """One Order/searchOpen call → list of open order dicts ([] on any failure).
    Shared by every consumer so a single flow never re-queries the same endpoint."""
    try:
        res = await client.post(
            "https://api.topstepx.com/api/Order/searchOpen",
            headers={"Authorization": f"Bearer {token}"},
            json={"accountId": account_id},
        )
        if res.status_code != 200:
            return []
        data   = res.json()
        orders = data.get("orders", []) if isinstance(data, dict) else data
        return orders if isinstance(orders, list) else []
    except Exception:
        return []


def _order_price(o: dict):
    """Working price of a bracket order: stopPrice for a stop, limit price for a TP."""
    if o.get("type") == 4:
        return o.get("stopPrice") or o.get("price")
    return o.get("price") or o.get("limitPrice")


def _split_bracket_by_side(orders: list, cid, side: int, ref: float, tick: float):
    """Classify this contract's open stop/limit orders against a live position on
    `side` (0=long, 1=short), using `ref` (the market/fill price) as the divider.

    A long's protective orders sit with the STOP below the market and the TP limit
    above it; a short's are mirrored. So any order on the OPPOSITE arrangement can
    only belong to the prior, now-reversed position — that's the reversal-orphan.
    A 1-tick buffer keeps a just-placed bracket or a break-even stop from being
    misread as wrong-side. Returns (ours_ids:set, orphan_ids:list)."""
    is_long = side == 0
    ours, orphan = set(), []
    for o in orders:
        if not isinstance(o, dict) or o.get("contractId") != cid or o.get("status") not in (1, 6):
            continue
        oid, otype = o.get("id"), o.get("type")
        if oid is None or otype not in (1, 4):
            continue
        p = _order_price(o)
        if p is None:
            continue
        p = float(p)
        if otype == 4:        # stop-loss
            wrong = (p > ref + tick) if is_long else (p < ref - tick)
        else:                 # take-profit limit
            wrong = (p < ref - tick) if is_long else (p > ref + tick)
        if wrong:
            orphan.append(oid)
        else:
            ours.add(oid)
    return ours, orphan


async def _cancel_ids(client, token, account_id, ids) -> int:
    """Cancel a list of order ids concurrently; returns how many succeeded.
    Order/cancel REQUIRES accountId in the body — without it the broker returns
    HTTP 400 and the order is never cancelled (this was the silent orphan bug)."""
    async def _c(oid):
        try:
            r = await client.post(
                "https://api.topstepx.com/api/Order/cancel",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"accountId": account_id, "orderId": oid},
            )
            # 200 = accepted (order cancelled or already gone). Anything else failed.
            return r.status_code == 200
        except Exception:
            return False
    if not ids:
        return 0
    return sum(1 for ok in await asyncio.gather(*(_c(i) for i in ids)) if ok)


def _tf_to_minutes(tf: str):
    """Parse a chart-timeframe string ('1m','5m','15m','1h','4h') into minutes.
    Returns None if it can't (→ structural self-monitor falls back to alert-only)."""
    if not tf:
        return None
    t = tf.strip().lower()
    try:
        if t.endswith("m"):
            return int(t[:-1])
        if t.endswith("h"):
            return int(t[:-1]) * 60
        if t.endswith("s"):
            return max(1, int(t[:-1]) // 60) or None
        return int(t)            # bare number = minutes
    except (ValueError, TypeError):
        return None


def _candle_bucket(tf: str, epoch: float):
    """Index of the candle containing `epoch` for timeframe `tf`. Intraday CME bars
    align to UTC minute boundaries, so floor(epoch / period) is the bar index and a
    change in the index means a candle just closed. None if tf isn't intraday."""
    mins = _tf_to_minutes(tf)
    if not mins:
        return None
    return int(epoch // (mins * 60))


async def cancel_all_orders(client, token, account_id, symbol) -> int:
    """Cancel every open/pending order for one instrument's contract.
    Must be called before flattening so bracket orders can't re-open a position.
    Cancels are issued CONCURRENTLY — during a flip, N sequential round-trips
    became one, shrinking the window where price can move against the reversal."""
    cid = (contracts.get(symbol) or {}).get("id")
    if not cid:
        return 0
    try:
        orders = await search_open_orders(client, token, account_id)
        contract_orders = [
            o for o in orders
            if isinstance(o, dict) and o.get("contractId") == cid and o.get("id")
        ]
        if not contract_orders:
            return 0

        async def _cancel(order_id):
            try:
                r = await client.post(
                    "https://api.topstepx.com/api/Order/cancel",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    # accountId is REQUIRED — omitting it returns HTTP 400 and the
                    # order is never cancelled (the silent orphan bug). With it, a
                    # 200 means cancelled or already-gone; 404 = already gone.
                    json={"accountId": account_id, "orderId": order_id},
                )
                return order_id, r.status_code
            except Exception:
                return order_id, None

        results = await asyncio.gather(*(_cancel(o["id"]) for o in contract_orders))
        cancelled = 0
        for order_id, code in results:
            if code == 200:
                cancelled += 1
            elif code == 404:
                # Already gone: an OCO sibling auto-cancelled when its TP/SL filled,
                # or it was cancelled already. Expected during a flatten — not an error.
                pass
            else:
                # 400 / other: a REAL failure (this masked the orphan bug for months).
                await alog(f"Failed to cancel order {order_id}: {code}", "warning")

        if cancelled > 0:
            await alog(f"Cancelled {cancelled} open order(s) for {symbol}", "system")
        return cancelled

    except Exception as e:
        await alog(f"Order cancellation error: {e}", "error")
        return 0


async def find_sl_orders(client, token, account_id, symbol, orders: list = None) -> list:
    """Return all open Stop-type (OrderType=4) order IDs for this symbol's contract.
    Called after a new entry to capture the SL bracket order ID, and on startup
    to arm BE state for positions that were already open before the bot started.
    Pass `orders` (a search_open_orders() snapshot) to skip the HTTP re-query."""
    cid = (contracts.get(symbol) or {}).get("id")
    if not cid:
        return []
    try:
        if orders is None:
            orders = await search_open_orders(client, token, account_id)
        # type=4 is Stop; status=1 is Open. Bracket SL orders are Stop-type.
        return [
            o["id"] for o in orders
            if isinstance(o, dict)
            and o.get("contractId") == cid
            and o.get("type") == 4
            and o.get("status") in (1, 6)   # Open or Pending
        ]
    except Exception as e:
        await alog(f"find_sl_orders error for {symbol}: {e}", "warning")
        return []


async def fetch_bracket_prices(client, token, account_id, symbol, orders: list = None):
    """Return (sl_price, tp_price) from the live open bracket orders for this symbol.
    Used at stacked-entry time so inherited levels reflect any manual SL/TP moves."""
    cid = (contracts.get(symbol) or {}).get("id")
    if not cid:
        return None, None
    try:
        if orders is None:
            orders = await search_open_orders(client, token, account_id)
        sl_p = tp_p = None
        for o in orders:
            if not isinstance(o, dict):
                continue
            if o.get("contractId") != cid:
                continue
            if o.get("status") not in (1, 6):
                continue
            if o.get("type") == 4:    # Stop = SL; broker uses stopPrice, not price
                p = o.get("stopPrice") or o.get("price")
                if p is not None:
                    sl_p = float(p)
            elif o.get("type") == 1:  # Limit = TP
                p = o.get("price") or o.get("limitPrice")
                if p is not None:
                    tp_p = float(p)
        return sl_p, tp_p
    except Exception as e:
        await alog(f"fetch_bracket_prices error for {symbol}: {e}", "warning")
        return None, None


async def enforce_bracket_levels(client, token, account_id, symbol, sl_abs, tp_abs,
                                 orders: list = None):
    """Force EVERY open stop → sl_abs and EVERY open TP (limit) → tp_abs for this
    instrument, so all stacked contracts sit on entry 1's EXACT levels regardless of
    fill slippage. This is the reliable replacement for tick-offset inheritance:
    instead of guessing the new entry's fill, we just move the real orders to the
    real target prices once they exist. Modifies run CONCURRENTLY (independent
    orders), and `orders` lets the caller pass an existing searchOpen snapshot."""
    cid = (contracts.get(symbol) or {}).get("id")
    if not cid or sl_abs is None:
        return
    tick = (contracts.get(symbol) or {}).get("tick") or 0.25
    sl_r = round(round(sl_abs / tick) * tick, 10)
    tp_r = round(round(tp_abs / tick) * tick, 10) if tp_abs is not None else None
    try:
        if orders is None:
            orders = await search_open_orders(client, token, account_id)
        bodies = []
        for o in orders:
            if not isinstance(o, dict) or o.get("contractId") != cid or o.get("status") not in (1, 6):
                continue
            if o.get("type") == 4:      # Stop (SL) → stopPrice
                bodies.append({"accountId": account_id, "orderId": o["id"], "stopPrice": sl_r})
            elif o.get("type") == 1 and tp_r is not None:    # Limit (TP) → limitPrice (skip when no TP given)
                bodies.append({"accountId": account_id, "orderId": o["id"], "limitPrice": tp_r})

        async def _modify(body):
            try:
                r = await client.post(
                    "https://api.topstepx.com/api/Order/modify",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
                return r.status_code == 200 and r.json().get("success")
            except Exception:
                return False

        results = await asyncio.gather(*(_modify(b) for b in bodies)) if bodies else []
        moved   = sum(1 for ok in results if ok)
        if moved:
            await alog(
                f"{symbol} INHERIT | unified {moved} bracket order(s) → SL {sl_r} / TP {tp_r}",
                "system",
                feed_msg=f"{symbol} SL/TP inherited",
            )
    except Exception as e:
        await alog(f"enforce_bracket_levels error for {symbol}: {e}", "warning")


async def place_stop_order(client, token, account_id, symbol, stop_price, pos_side, size):
    """Place a standalone protective stop that flattens `size` if price hits
    stop_price. Used by Auto-BE in two-phase PHASE 2, where the original hard stop
    has been removed and there is no bracket stop left to move. Returns the new
    order id on success, else None."""
    cid = (contracts.get(symbol) or {}).get("id")
    if not cid:
        return None
    tick = (contracts.get(symbol) or {}).get("tick") or 0.25
    sp   = round(round(stop_price / tick) * tick, 10)
    close_side = 1 if pos_side == 0 else 0   # sell-stop closes a long; buy-stop a short
    try:
        r = await client.post(
            "https://api.topstepx.com/api/Order/place",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"accountId": account_id, "contractId": cid, "type": 4,
                  "side": close_side, "size": size, "stopPrice": sp},
        )
        if r.status_code == 200 and r.json().get("success"):
            return r.json().get("orderId")
    except Exception:
        pass
    return None


async def trigger_breakeven(client, token, account_id, symbol):
    """Move all open SL orders for this symbol to avg_price ± 1 tick (break even
    plus one tick to cover commissions). Fires at most once per position build —
    be_state[symbol]['be_triggered'] is set to True after the first successful call."""
    state = be_state.get(symbol)
    if not state or state.get("be_triggered"):
        return
    pos = positions.get(symbol) or {}
    avg = pos.get("avg_price")
    direction = pos.get("direction")
    if avg is None or direction not in ("LONG", "SHORT"):
        return
    # Mark the trade as having reached break-even protection (for outcome tagging),
    # regardless of whether the stop-move below succeeds or we end up flattening.
    _stamp_open_rows(symbol, be_fired=True)
    # A+ trade moving to break-even → ping (A+-only). The trade is now risk-free.
    if any(e.get("a_plus") and not e.get("separator") and e.get("instrument") == symbol
           and e.get("pnl") == "—" for e in trade_log):
        asyncio.create_task(notify_push(
            f"⚖️ A+ {symbol} moved to break-even · {now_ny()} NY", mention=True,
            embed=_embed(f"⚖️ A+ moved to Break-Even — {symbol}", "be",
                         fields=[("Instrument", symbol), ("Stop", "→ break-even (risk-free)")],
                         footer=f"{now_ny()} NY")))
    tick = (contracts.get(symbol) or {}).get("tick") or 0.25
    # One tick above avg for LONG (guarantees positive fill); one tick below for SHORT.
    be_price = round(avg + tick, 10) if direction == "LONG" else round(avg - tick, 10)
    # Round to nearest tick to avoid broker rejection on sub-tick prices
    be_price = round(round(be_price / tick) * tick, 10)

    # Current price (robust to a thin feed).
    _q   = quotes.get(symbol) or {}
    _bid, _ask = _q.get("bid"), _q.get("ask")
    last = _q.get("last") or ((_bid + _ask) / 2 if (_bid is not None and _ask is not None) else (_ask or _bid))

    # If price has ALREADY rolled back to break-even, a BE stop can't be placed on
    # the correct side of the market — the broker rejects it ("Invalid stop price").
    # Per the rule: don't fight it, just FLATTEN now and lock the break-even (never
    # give a winner back). Mark be_triggered first so the monitor stops retrying.
    rolled_back = last is not None and (
        (direction == "LONG" and last <= be_price) or
        (direction == "SHORT" and last >= be_price)
    )
    if rolled_back:
        state["be_triggered"] = True
        await alog(f"BE | {symbol} price back at break-even — flattening to lock it", "warning",
                   feed_msg=f"BE flatten · {symbol}")
        await flatten_symbol(symbol, reason="break-even reached")
        return

    # Use only the stops that are ACTUALLY live right now. The old code fell back to
    # the stored ids, which after a two-phase phase-2 transition point at the already
    # CANCELLED hard stop — modifying it returns "Order not found" (errorCode 3), which
    # the code mis-read as "Invalid stop price" and flattened the trade early. Fixed:
    # if there's a live stop we move it; if not (phase 2), we PLACE a fresh BE stop.
    live_ids = await find_sl_orders(client, token, account_id, symbol)
    if live_ids:
        state["sl_order_ids"] = live_ids

        async def _modify_sl(oid):
            """Returns (success, invalid_price_flag) for one SL order. ONLY the real
            'Invalid stop price' message counts as price-passed-BE — not a generic
            errorCode/Order-not-found, which just means that stop is already gone."""
            try:
                res = await client.post(
                    "https://api.topstepx.com/api/Order/modify",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"accountId": account_id, "orderId": oid, "stopPrice": be_price},
                )
                if res.status_code == 200 and res.json().get("success"):
                    return True, False
                inv = "Invalid stop price" in res.text
                await alog(f"BE modify failed for order {oid} ({symbol}): {res.status_code} — {res.text}", "warning")
                return False, inv
            except Exception as e:
                await alog(f"BE modify error for order {oid} ({symbol}): {e}", "warning")
                return False, False

        results       = await asyncio.gather(*(_modify_sl(oid) for oid in live_ids))
        success_count = sum(1 for ok, _ in results if ok)
        invalid_price = any(inv for _, inv in results)

        if success_count > 0:
            state["be_triggered"] = True
            await alog(
                f"✅ BREAK EVEN SET | {symbol} | SL moved to {be_price} (avg {avg} ± 1 tick) | "
                f"{success_count}/{len(live_ids)} order(s) modified",
                "success",
                feed_msg=f"BE triggered · {symbol}",
            )
            return
        if invalid_price:
            # Price genuinely passed break-even → flatten to lock it.
            state["be_triggered"] = True
            await alog(f"BE | {symbol} stop rejected at break-even — flattening to lock it", "warning",
                       feed_msg=f"BE flatten · {symbol}")
            await flatten_symbol(symbol, reason="break-even reached")
            return
        # else (e.g. the stop vanished) → fall through and place a fresh one.

    # No live stop to move (two-phase phase 2, hard stop removed) → PLACE a fresh
    # break-even stop so the trade is risk-free from here, exactly as intended.
    new_id = await place_stop_order(client, token, account_id, symbol, be_price,
                                    0 if direction == "LONG" else 1, pos.get("size", 0))
    if new_id:
        state["be_triggered"] = True
        state["sl_order_ids"] = [new_id]
        current_bracket_ids.setdefault(symbol, set()).add(new_id)
        await alog(
            f"✅ BREAK EVEN SET | {symbol} | placed break-even stop @ {be_price} (phase 2)",
            "success",
            feed_msg=f"BE triggered · {symbol}",
        )
    else:
        # Couldn't place a stop — do NOT flatten early; ride the close-based exit.
        state["be_triggered"] = True
        await alog(f"BE | {symbol} couldn't place a break-even stop — riding the close-based exit", "warning")


async def flatten_position(client, token, account_id, symbol, current_size, current_side):
    flatten_side = 1 if current_side == 0 else 0
    direction    = "SELL" if flatten_side == 1 else "BUY"
    cid          = (contracts.get(symbol) or {}).get("id")
    payload = {
        "accountId":  account_id,
        "contractId": cid,
        "type":       2,
        "side":       flatten_side,
        "size":       current_size,
    }
    response = await client.post(
        "https://api.topstepx.com/api/Order/place",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if response.status_code == 401:
        # Cached token went stale mid-flight — re-auth once and retry the flatten.
        token, account_id = await get_auth(client, force=True)
        if token:
            payload["accountId"] = account_id
            response = await client.post(
                "https://api.topstepx.com/api/Order/place",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
    if response.status_code == 200:
        positions[symbol] = blank_pos()
        await alog(f"FLATTENED {symbol} — {current_size} contract(s) {direction} closed", "system",
                   feed_msg=f"Flatten · {symbol} {current_size}")
        return True
    else:
        await alog(f"FLATTEN FAILED | {response.status_code} — {response.text}", "error")
        return False


async def place_order(client, token, account_id, symbol, side, current_size,
                      ifvg_info: str = None, sl_price: float = None, entry_ref: float = None,
                      timeframe: str = None, c1_high: float = None, c1_low: float = None,
                      swing_sl: float = None, a_plus: bool = False, a_plus_target: float = None):
    global session_stats
    direction = "BUY" if side == 0 else "SELL"
    mode_lbl  = _stop_mode_label(trade_settings)   # stop mode, for log/feed messages
    _qty      = qty_for(symbol)        # contracts this entry adds (per-instrument)
    _cap      = max_contracts_for(symbol)
    _adaptive_this_trade = False       # set True below when adaptive mode sizes this order
    contract   = contracts.get(symbol) or {}
    tick       = contract.get("tick") or 0.25
    # Robust price reference for SL/TP math. `last` is best, but on a thin feed it
    # can be None, so fall back to the bid/ask midpoint, then either side, then the
    # existing position's average (always present on a stacked entry). This keeps
    # stacked-entry inheritance working even when no trade has printed yet.
    _q   = quotes.get(symbol) or {}
    _mid = ((_q.get("bid") + _q.get("ask")) / 2
            if (_q.get("bid") is not None and _q.get("ask") is not None) else None)
    _est_entry = (_q.get("last") or _mid or _q.get("ask") or _q.get("bid")
                  or (positions.get(symbol) or {}).get("avg_price"))   # best price estimate at order time

    def pts_to_ticks(pts):
        return max(MIN_SL_TICKS, round(pts / tick))

    # A+ setups take a STRUCTURAL stop (the two-phase candle-1 IFVG stop) regardless
    # of the live stop mode, and a TP that rides to the near edge of the unfilled HTF
    # FVG the setup targets (a_plus_target from Pine) — see the TP block below. Swing
    # is force-disabled for A+.
    structural = True if a_plus else bool(trade_settings.get("structural_sl"))
    safety_pts = SAFETY_NET_PTS.get(symbol, 40)
    struct_tp  = trade_settings.get("structural_tp", "3x")

    # ── TP fallback base (only used if struct_tp isn't a valid multiplier) ─────
    tp_pts    = tp_pts_for(symbol)
    tp_t      = max(1, round(tp_pts / tick))
    tp_source = ("TP.C" if symbol in CRUDE_SYMBOLS else
                 "TP.1" if symbol in TP1_SYMBOLS else "TP.2")

    # ── Two-phase structural SL — ONE level: the IFVG invalidation level ───────
    # That level is candle-1's HIGH for a long, candle-1's LOW for a short — i.e.
    # exactly what the alert already sends as sl_price (high[1] / low[1]). The PHASE
    # only changes the trigger, never the level:
    #   Phase 1 (entry candle): a HARD broker stop sitting AT the level. Any touch
    #     through it = out — if price slips back through the IFVG level inside the
    #     first candle the imbalance never formed (a "paper cut", which is fine).
    #   Phase 2 (after the entry candle closes): structural_monitor_loop removes the
    #     hard stop and exits only when a candle CLOSES beyond that same level —
    #     wicks through it are allowed, giving the runner room. (Auto-BE may move the
    #     stop to break-even during phase 1; if so it's kept into phase 2.)
    # Activates only when the level is on the LOSS side of the (estimated) entry,
    # which the IFVG entry rule guarantees; otherwise we fall back to the safety net.
    # ── Swing stop — the 15-bar low (long) / high (short) sent by Pine ──────────
    # When Swing Stop is on, the protective stop sits at `swing_sl` and STAYS there
    # as a plain hard bracket stop for the life of the trade: no candle-1 close-based
    # early exit (structural_pos is left unset below), so the trade has full room to
    # its structural stop or TP. Falls back to the candle-1 two-phase logic if the
    # swing level is missing or isn't on the loss side.
    swing_mode  = False if a_plus else bool(trade_settings.get("swing_stop"))

    def _on_loss(level):
        return (level is not None and _est_entry is not None
                and ((level < _est_entry) if side == 0 else (level > _est_entry)))

    _swing_ok     = _on_loss(swing_sl)
    _candle_ok    = _on_loss(sl_price)
    _swing_level  = float(swing_sl) if _swing_ok else None
    _candle_level = float(sl_price) if _candle_ok else None

    _two_phase     = False    # entry stop = candle-1 IFVG level (hard → phase 2)
    _swing_used    = False    # entry stop = swing 15-bar level (plain hard, whole trade)
    _hard_level    = _close_level = None
    if structural:
        # Candle-1 two-phase; safety net if the level is missing/unusable.
        if _candle_ok:
            _two_phase = True; _hard_level = _close_level = _candle_level
    elif swing_mode:
        # Plain swing stop; fall back to candle-1 two-phase if no swing level.
        if _swing_ok:
            _swing_used = True
        elif _candle_ok:
            _two_phase = True; _hard_level = _close_level = _candle_level

    # ── SL ────────────────────────────────────────────────────────────────────
    if _swing_used and current_size == 0:
        sl_dist = abs(_est_entry - _swing_level)
        sl_t    = max(MIN_SL_TICKS, round(sl_dist / tick)); sl_pts = round(sl_t * tick, 2)
        sl_source = f"swing 15-bar @ {_swing_level}"
    elif _two_phase and current_size == 0:
        sl_dist = abs(_est_entry - _hard_level)
        sl_t    = max(MIN_SL_TICKS, round(sl_dist / tick)); sl_pts = round(sl_t * tick, 2)
        sl_source = f"structural hard @ IFVG level ({_hard_level})"
    elif structural:
        sl_pts    = safety_pts
        sl_t      = pts_to_ticks(sl_pts); sl_pts = round(sl_t * tick, 2)
        sl_source = f"structural · safety {safety_pts}pt"
    else:
        sl_pts    = safety_pts
        sl_t      = pts_to_ticks(sl_pts); sl_pts = round(sl_t * tick, 2)
        sl_source = "fixed"

    # ── TP ─────────────────────────────────────────────────────────────────────
    # A+ → ride to the near edge of the unfilled HTF FVG the setup targets
    # (a_plus_target from Pine: the gap's bottom for a long, top for a short). Fall
    # back to the fixed per-instrument distance (50pt NQ/MNQ, 15pt the rest) only when
    # no usable level was sent (target 0 / on the wrong side after slippage). Non-A+ →
    # N× the protective-stop distance (2x/3x/5x/7x), or the flat TP.1/TP.2 base.
    _mult = STRUCT_TP_MULT.get(struct_tp)
    if a_plus and current_size == 0:
        _fb_pts = aplus_tp_fallback_pts(symbol)
        _tgt_ok = (a_plus_target is not None and a_plus_target > 0 and _est_entry is not None
                   and ((a_plus_target > _est_entry) if side == 0 else (a_plus_target < _est_entry)))
        if _tgt_ok:
            tp_t      = max(1, round(abs(a_plus_target - _est_entry) / tick)); tp_pts = round(tp_t * tick, 2)
            tp_source = f"HTF FVG edge @ {a_plus_target}"
        else:
            tp_t      = max(1, round(_fb_pts / tick)); tp_pts = round(tp_t * tick, 2)
            tp_source = f"A+ fallback {_fb_pts}pt"
    elif _mult and current_size == 0:
        tp_pts    = round(sl_pts * _mult, 2)
        tp_t      = max(1, round(tp_pts / tick))
        tp_source = f"{struct_tp} of stop (={tp_pts}pt)"

    sl_ticks = -sl_t if side == 0 else sl_t
    tp_ticks = tp_t if side == 0 else -tp_t

    # ── Stacked-entry: inherit live SL/TP abs prices ──────────────────────────
    # Any mode: when adding to an existing position (same direction), reuse the
    # exact SL and TP prices from the live broker orders (not stored estimates) so
    # any manual adjustments made since entry 1 are automatically followed.
    # Falls back to stored orig_sl/tp_price if the live query returns nothing.
    _is_stacked   = current_size > 0
    _prior        = be_state.get(symbol) or {}
    _inherit_sl   = None      # entry-1's exact SL/TP, enforced on every order post-fill
    _inherit_tp   = None
    if _is_stacked:
        _live_sl, _live_tp = await fetch_bracket_prices(client, token, account_id, symbol)
        _isl = _live_sl if _live_sl is not None else _prior.get("orig_sl_price")
        _itp = _live_tp if _live_tp is not None else _prior.get("orig_tp_price")
        if _isl is not None and _itp is not None:
            _inherit_sl, _inherit_tp = _isl, _itp     # used to force-unify after the fill
            # Best-effort attached bracket (approximate, off an estimated fill); the
            # post-fill enforce_bracket_levels() corrects it to the exact level.
            _ref = _est_entry if _est_entry is not None else (_isl + _itp) / 2
            sl_t     = max(MIN_SL_TICKS, round(abs(_isl - _ref) / tick))
            sl_pts   = round(sl_t * tick, 2)
            sl_ticks = -sl_t if side == 0 else sl_t
            tp_t     = max(1, round(abs(_itp - _ref) / tick))
            tp_pts   = round(tp_t * tick, 2)
            tp_ticks = tp_t if side == 0 else -tp_t
            sl_source = f"inherited ({_isl:.2f})"
            tp_source = f"inherited ({_itp:.2f})"
        else:
            await alog(f"{symbol} stacked entry — no live/stored SL/TP, using fresh brackets", "warning")

    # ── ADAPTIVE MODE: fixed per-instrument geometry + ladder size ────────────
    # When adaptive mode is on for this micro, override the size and the stop/target
    # with the engine's values and force a PLAIN hard bracket (no structural /
    # two-phase / swing close-based exit). No-op when adaptive mode is off. Fresh
    # entries only — adaptive mode never stacks (single-position lock guarantees it).
    if adaptive_active_for(symbol) and current_size == 0 and not a_plus:
        _geo = adaptive.geometry_for(adaptive_settings, symbol)
        if _geo:
            _stop_pts, _tp_pts = _geo
            _qty      = adaptive_qty(symbol)
            sl_t      = pts_to_ticks(_stop_pts)
            tp_t      = max(1, round(_tp_pts / tick))
            sl_pts    = round(sl_t * tick, 2)
            tp_pts    = round(tp_t * tick, 2)
            sl_ticks  = -sl_t if side == 0 else sl_t
            tp_ticks  = tp_t if side == 0 else -tp_t
            sl_source = f"adaptive fixed {sl_pts}pt"
            tp_source = f"adaptive fixed {tp_pts}pt"
            structural = _two_phase = _swing_used = False   # plain bracket, no structural exit
            _adaptive_this_trade = True                      # → skip auto-BE (fixed 20/50 outcome)
            await alog(f"{symbol} ADAPTIVE — {_qty}× micro · stop {sl_pts}pt / TP {tp_pts}pt "
                       f"(mult {adaptive_state.get('mult', 1.0):.2f})", "system",
                       feed_msg=f"Adaptive · {symbol} {_qty}× {sl_pts}/{tp_pts}pt")

    payload = {
        "accountId":         account_id,
        "contractId":        contract.get("id"),
        "type":              2,
        "side":              side,
        "size":              _qty,
        "stopLossBracket":   {"ticks": sl_ticks, "type": 4},
        "takeProfitBracket": {"ticks": tp_ticks, "type": 1},
    }
    response = await client.post(
        "https://api.topstepx.com/api/Order/place",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if response.status_code == 401:
        # Cached token went stale mid-flight — re-auth once and retry the entry.
        token, account_id = await get_auth(client, force=True)
        if token:
            payload["accountId"] = account_id
            response = await client.post(
                "https://api.topstepx.com/api/Order/place",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
    if response.status_code == 200:
        new_size      = current_size + _qty
        direction_str = "LONG" if side == 0 else "SHORT"
        prev_avg = (positions.get(symbol) or {}).get("avg_price")
        # Optimistic avg until the authoritative SignalR GatewayUserPosition event
        # lands (~200-500ms). Old code wrote prev_avg — which is None on a fresh
        # entry and the stale pre-add average on a stack — so the BE monitor could
        # work off a wrong/empty average in that gap (BUG-3). Estimate the fill from
        # the live quote instead: fresh entry → the est fill; stacked add → the
        # size-weighted blend of the prior avg and the est fill.
        _est_fill = (quotes.get(symbol) or {}).get("last")
        if current_size == 0:
            _opt_avg = _est_fill if isinstance(_est_fill, (int, float)) else prev_avg
        elif isinstance(prev_avg, (int, float)) and isinstance(_est_fill, (int, float)):
            _opt_avg = round((prev_avg * current_size + _est_fill * _qty) / new_size, 4)
        else:
            _opt_avg = prev_avg
        positions[symbol] = {"size": new_size, "side": side, "direction": direction_str, "avg_price": _opt_avg}
        # Track whether this position is structural (honours the close-based
        # invalidation exit). Set on the fresh entry; stacked adds keep it. We also
        # store the candle-1 invalidation level (sl_price from Pine) and the entry
        # candle index so the bot can self-monitor closes against it — see
        # structural_monitor_loop(). With no level we fall back to alert-only.
        if current_size == 0:
            if _swing_used:
                # Swing stop is a plain hard bracket stop at the 15-bar extreme. We
                # deliberately register NO structural position, so the candle-1
                # close-based invalidation exit never fires and the trade keeps full
                # room to its structural stop / TP (incoming exit alerts no-op).
                structural_pos.pop(symbol, None)
            elif structural or _two_phase:
                # Structural mode, OR a swing-mode entry that fell back to the
                # candle-1 structural stop (_two_phase True with the toggle off) —
                # either way the close-based invalidation exit must be armed.
                # close-based invalidation level: the candle-1 extreme on the
                # entry side (= _close_level for two-phase, else the alert sl_price).
                if _two_phase:
                    _lvl = _close_level
                else:
                    try:
                        _lvl = float(sl_price) if sl_price is not None else None
                    except (ValueError, TypeError):
                        _lvl = None
                structural_pos[symbol] = {
                    "tf":           (timeframe or ""),
                    "level":        _lvl,            # phase-2 close-beyond exit level
                    "hard_level":   _hard_level,     # phase-1 hard stop (None if not two-phase)
                    "two_phase":    _two_phase,
                    "phase":        1 if _two_phase else 2,
                    "side":         side,
                    "entry_bucket": _candle_bucket(timeframe or "", time.time()),
                }
            else:
                structural_pos.pop(symbol, None)
        session_stats["orders_placed"] += 1
        if side == 0: session_stats["buys"] += 1
        else:         session_stats["sells"] += 1

        ifvg_prefix = f"{ifvg_info}  " if ifvg_info else ""
        file_msg    = f"{ifvg_prefix}{direction} {_qty} {symbol} [{mode_lbl}]"
        feed_msg    = f"{ifvg_info} {_qty} {symbol}" if ifvg_info else f"{direction} {_qty} {symbol}"
        event_type  = "buy" if side == 0 else "sell"
        await alog(file_msg, event_type, ifvg_info, feed_msg=feed_msg)
        await alog(
            f"ORDER PLACED | {symbol} {direction} | Position: {new_size}/{_cap} | SL: {sl_pts}pt [{sl_source}] | TP: {tp_pts}pt [{tp_source}]",
            "success",
            feed_msg=f"{symbol} {direction} · {new_size}/{_cap} · SL {sl_pts} TP {tp_pts}",
        )

        # A+ setup → instant push alert (Discord + Telegram, whichever is configured;
        # swing stop, allowed to exceed the max-stop cap). No-op if no channel is set.
        if a_plus:
            _aptxt = (f"🅰️ A+ SETUP TAKEN\n{symbol} {direction} x{_qty} @ ~{_est_entry}\n"
                      f"Structural SL {sl_pts}pt · TP {tp_pts}pt [{tp_source}]\n"
                      f"{(ifvg_info + ' · ') if ifvg_info else ''}{now_ny()} NY")
            asyncio.create_task(notify_push(_aptxt, mention=True, embed=_embed(
                f"🅰️ A+ Setup Taken — {symbol} {direction}", "aplus",
                fields=[("Size", f"{_qty}"), ("Entry", f"~{_est_entry}"),
                        ("Structural SL", f"{sl_pts}pt"), ("TP", f"{tp_pts}pt")],
                footer=f"{ifvg_info or 'IFVG'} · {now_ny()} NY")))

        # Add to trade log — entry-time row. Outcome + P&L are filled on close.
        trade_entry = {
            "time_ist":    now_ist(),                 # ENTRY time (IST)
            "time_ny":     now_ny(),                  # ENTRY time (NY / exchange)
            "session":     _ny_session_id(),          # 6 PM-NY session bucket
            "instrument":  symbol,
            "direction":   direction,
            "size":        f"{_qty}",
            "ifvg_source": ifvg_info or "—",
            "stop_mode":   ("A+ structural" if a_plus else _stop_mode_label(trade_settings)),
            "a_plus":      bool(a_plus),
            "sl_tp":       f"{sl_pts}pt / {tp_pts}pt",
            "sl_pts":      sl_pts,
            "tp_pts":      tp_pts,
            "pnl":         "—",
            "outcome":     "—",
            "be_fired":       False,   # auto-BE moved the stop to break-even
            "reached_phase2": False,   # survived the entry candle (structural phase-2)
            "exit_reason":    None,    # set when WE flatten (close-based / BE / manual)
            "result_logged":  False,
        }
        trade_log.appendleft(trade_entry)
        save_state()
        # Broadcast trade log update via SSE
        await alog("__trade_log_update__", "trade_log_update")

        # ── Wait (bounded) for the broker to create the new bracket ────────────
        # Replaces the old fixed 1.2s + 1.5s sleeps (held under trade_lock):
        # poll Order/searchOpen with a short backoff until a NEW stop order id
        # appears, then reuse that ONE snapshot for both the stacked-entry
        # inheritance and the BE arming below. Typical wait drops to ~0.3-0.7s;
        # worst case ~2s (same ballpark as before). Trade-off: 1-3 extra
        # searchOpen reads per entry.
        # Always poll now: besides BE/inheritance, we need the new bracket's order
        # ids to capture the current bracket set and sweep reversal-orphans.
        _needs_enforce = _is_stacked and _inherit_sl is not None and _inherit_tp is not None
        _prior_ids     = set((be_state.get(symbol) or {}).get("sl_order_ids", []))
        _orders_snap   = None
        new_sl_ids     = []
        for _d in (0.3, 0.4, 0.5, 0.8):
            await asyncio.sleep(_d)
            _orders_snap = await search_open_orders(client, token, account_id)
            new_sl_ids   = await find_sl_orders(client, token, account_id, symbol, orders=_orders_snap)
            if set(new_sl_ids) - _prior_ids:   # the NEW entry's SL bracket exists
                break

        # ── Two-phase fresh entry: pin the hard stop to the EXACT candle-1 level ──
        # The attached bracket is a tick offset off an ESTIMATED entry; correct it to
        # the real candle-1 price (and re-derive the TP off the exact hard distance
        # for 2x/3x) so the phase-1 stop sits precisely where the rule wants it.
        if _two_phase and current_size == 0 and _orders_snap is not None and _hard_level is not None:
            _fill = (positions.get(symbol) or {}).get("avg_price") or _est_entry
            if _fill is not None:
                _m = STRUCT_TP_MULT.get(struct_tp)
                if _m:
                    _hard_dist = abs(_fill - _hard_level)
                    _tp_abs = _fill + _m * _hard_dist if side == 0 else _fill - _m * _hard_dist
                else:
                    _tp_abs = _fill + tp_pts if side == 0 else _fill - tp_pts
                await enforce_bracket_levels(client, token, account_id, symbol,
                                             _hard_level, _tp_abs, orders=_orders_snap)
                _orders_snap = await search_open_orders(client, token, account_id)

        # ── Stacked entry: force EVERY bracket to entry-1's EXACT SL/TP ────────
        # The attached bracket above is only approximate (it's a tick offset off an
        # estimated fill). Now that the new bracket exists, modify all of this
        # instrument's stops/TPs to the exact inherited levels so every contract —
        # old and new — sits on identical SL and TP prices. This is the reliable
        # inheritance the previous tick-offset approach could never quite hit.
        if _needs_enforce:
            await enforce_bracket_levels(client, token, account_id, symbol,
                                         _inherit_sl, _inherit_tp, orders=_orders_snap)
            _orders_snap = await search_open_orders(client, token, account_id)  # refresh post-modify

        # ── Capture the current bracket + sweep reversal-orphans ───────────────
        # Split the contract's open orders by side vs the fill price: the orders on
        # the CORRECT side for this new position are its bracket (recorded so later
        # sweeps never touch them); anything on the WRONG side can only be the prior,
        # now-reversed position's leftover SL/TP — cancel it. This is the orphan the
        # flat-based sweep can't see, because the contract now holds a live position.
        if _orders_snap is not None:
            _cid  = (contracts.get(symbol) or {}).get("id")
            _ref  = ((positions.get(symbol) or {}).get("avg_price")
                     or _est_entry
                     or (quotes.get(symbol) or {}).get("last"))
            _tick = (contracts.get(symbol) or {}).get("tick") or 0.25
            if _ref is not None:
                _ours, _orphans = _split_bracket_by_side(_orders_snap, _cid, side, float(_ref), _tick)
                if current_size == 0:
                    current_bracket_ids[symbol] = set(_ours)
                else:
                    current_bracket_ids[symbol] = set(current_bracket_ids.get(symbol, set())) | _ours
                if _orphans:
                    _killed = await _cancel_ids(client, token, account_id, _orphans)
                    if _killed:
                        await alog(
                            f"ORPHAN SWEEP | {symbol} | cancelled {_killed} stale wrong-side order(s) "
                            f"left from a reversal",
                            "warning",
                            feed_msg=f"Orphans cleared · {symbol} ({_killed})",
                        )

        # ── Arm break-even tracking (only when Auto-BE@50% is enabled) ─────────
        # Adaptive trades never arm BE — they run a pure fixed bracket (stop/target
        # exactly as configured) so every outcome is a clean win or loss for the ladder.
        if trade_settings.get("auto_be") and not _adaptive_this_trade:
            prev_state  = be_state.get(symbol) or {}
            # Merge: keep any prior IDs not in the new list (handles stacked entries
            # where the old bracket is still open alongside the new one), then add
            # the new ones. Deduplicate so we never modify the same order twice.
            merged_ids  = list({*prev_state.get("sl_order_ids", []), *new_sl_ids})
            _is_fresh   = current_size == 0
            # By the time the bracket poll has seen the SL order, the fill has usually
            # arrived in positions[symbol]["avg_price"]. Use that as the price
            # reference — it is the actual fill, more reliable than _est_entry which
            # was captured before the order was placed (falls back to it if not yet in).
            _fill_price = (positions.get(symbol) or {}).get("avg_price")
            _price_est  = _fill_price if _fill_price is not None else _est_entry
            # Store/preserve original abs SL/TP for stacked-entry inheritance.
            # Fresh entry: structural sl_price is exact; others estimate from fill price.
            # Stacked entry: copy originals from prev_state — never overwrite.
            if _is_fresh:
                _sl_abs = (
                    (_price_est - sl_pts if side == 0 else _price_est + sl_pts)
                    if _price_est is not None else None
                )
                _tp_abs = (
                    (_price_est + tp_pts if side == 0 else _price_est - tp_pts)
                    if _price_est is not None else None
                )
            else:
                _sl_abs = prev_state.get("orig_sl_price")
                _tp_abs = prev_state.get("orig_tp_price")
            # BE trigger uses original tp_pts from entry 1; stacked entries preserve it.
            _be_tp_pts  = tp_pts if _is_fresh else prev_state.get("tp_pts", tp_pts)
            be_state[symbol] = {
                "sl_order_ids":  merged_ids,
                "be_triggered":  False,
                "tp_pts":        _be_tp_pts,
                "mode":          trading_mode,
                "orig_sl_price": _sl_abs,
                "orig_tp_price": _tp_abs,
            }
            if merged_ids:
                await alog(
                    f"BE armed | {symbol} | trigger at {round(_be_tp_pts / 2, 2)}pt profit | "
                    f"SL order(s): {merged_ids}",
                    "system",
                    feed_msg=f"BE armed · {symbol}",
                )
            else:
                await alog(
                    f"BE armed | {symbol} | SL order ID not yet found — monitor will retry",
                    "warning",
                    feed_msg=f"BE arming · {symbol}",
                )

    else:
        session_stats["orders_failed"] += 1
        await alog(f"ORDER FAILED | {symbol} {direction} | {response.status_code} — {response.text}", "error")


# ─── Trade Execution ──────────────────────────────────────────────────────────
async def confirm_flat(client, token, account_id, symbol, attempts: int = 6, delay: float = 0.5) -> bool:
    """Poll the broker until one instrument's position is actually 0 after a
    flatten. HTTP 200 on Order/place only means 'accepted', not 'filled' — so we
    verify the fill landed before reversing, instead of trusting a fixed sleep."""
    for _ in range(attempts):
        size, _side = await refresh_positions(client, token, account_id, only_symbol=symbol)
        if size == 0:
            return True
        await asyncio.sleep(delay)
    return False


async def execute_trade(symbol: str, action: str, ifvg_info: str = None, is_test: bool = False,
                        sl_price: float = None, entry_ref: float = None, timeframe: str = None,
                        c1_high: float = None, c1_low: float = None, swing_sl: float = None,
                        a_plus: bool = False, a_plus_target: float = None):
    global session_stats
    side      = 0 if action.lower() == "buy" else 1
    direction = "BUY" if side == 0 else "SELL"
    prefix    = "[TEST] " if is_test else ""

    if bot_paused and not is_test:
        await alog(f"{prefix}PAUSED — {symbol} signal ignored ({direction})", "warning", ifvg_info)
        return

    # One signal at a time across ALL instruments. Queued signals run strictly
    # sequentially, so each reads the true position left by the previous one.
    async with trade_lock:
      async with api_client() as client:
        token, account_id = await get_auth(client)
        if not token or not account_id:
            await alog(f"{prefix}{symbol} trade aborted — auth failed", "error")
            return

        # Make sure we know the live contract for this instrument before trading
        # (auto-resolves / handles quarterly rollover).
        await ensure_contracts(client, token)
        if not (contracts.get(symbol) or {}).get("id"):
            await alog(f"{prefix}{symbol} trade aborted — could not resolve contract", "error")
            return

        # Full read across the WHOLE no-hedge group (every enabled instrument),
        # now that refresh_positions reads type/size correctly.
        await refresh_positions(client, token, account_id)
        sig_pos = positions.get(symbol) or blank_pos()

        # ── ADAPTIVE MODE GATE (opt-in; no-op when adaptive_enabled is False) ──
        # Micros only, ONE position account-wide, and a hard daily-loss stop. These
        # checks run before the normal signal handling and only when the mode is on.
        if adaptive_enabled and not is_test:
            if adaptive_state.get("stopped"):
                await alog(f"{symbol} IGNORED — adaptive daily-loss stop active (manual resume required)",
                           "warning", feed_msg=f"Adaptive stop · {symbol} {direction} ignored")
                return
            if symbol not in ADAPTIVE_MICROS:
                await alog(f"{symbol} IGNORED — adaptive mode trades micros only "
                           f"({', '.join(ADAPTIVE_MICROS)})", "warning",
                           feed_msg=f"Adaptive skip · {symbol} not a micro")
                return
            if sig_pos["direction"] == "FLAT" and _any_position_open():
                _open = [s for s in enabled_symbols
                         if (positions.get(s) or {}).get("direction", "FLAT") != "FLAT"]
                await alog(f"{symbol} IGNORED — adaptive single-position lock "
                           f"({', '.join(_open)} still open); waiting for flat",
                           "warning", feed_msg=f"Adaptive lock · {symbol} held")
                return

        if is_test:
            sl_desc = "structural (close-based + safety net)" if trade_settings.get("structural_sl") else f"fixed {SAFETY_NET_PTS.get(symbol, 40)}pt"
            await alog(
                f"[TEST] API OK — would {direction} {qty_for(symbol)}× {symbol} | position={sig_pos['size']}/{max_contracts_for(symbol)} | mode={MODES[trading_mode]['label']} | SL={sl_desc}",
                "signal",
            )
            return

        # ── SIGNAL HANDLING (ignore opposing; never flatten, never flip) ──────
        # Rule set:
        #   • This instrument already on the OPPOSING side → IGNORE the signal.
        #     The open position is LEFT ALONE to close on its own (TP / stop /
        #     break-even / structural exit / manual flatten). No flatten, no flip.
        #   • This instrument on the SAME side → IGNORE (stacking removed; one
        #     position per signal).
        #   • This instrument FLAT → open a fresh position, UNLESS a same-no-hedge-
        #     group instrument is already open on the opposite side, in which case
        #     the counter-group signal is IGNORED (hedging is not allowed).
        grp     = hedge_group(symbol)
        sig_now = positions.get(symbol) or blank_pos()

        # Opposing position on THIS instrument → IGNORE (keep the existing trade).
        if sig_now["direction"] != "FLAT" and sig_now["side"] != side:
            await alog(
                f"{symbol} IGNORED — opposing {direction} signal while {sig_now['size']}× "
                f"{sig_now['direction']} is open (no flatten, no flip)",
                "warning",
                feed_msg=f"Opposing ignored · {symbol} {sig_now['direction']} held",
            )
            return

        # Same-side position → IGNORE (stacking removed — one position per signal).
        if sig_now["direction"] != "FLAT" and sig_now["side"] == side:
            await alog(f"{symbol} IGNORED — already {sig_now['size']}× {direction} (no stacking)", "warning",
                       feed_msg=f"Already in · {symbol} {sig_now['size']}× {direction}")
            return

        # FLAT → open fresh, but block a within-group hedge first.
        group_opp = [
            s for s in enabled_symbols
            if s in grp and s != symbol
            and (positions.get(s) or blank_pos())["direction"] != "FLAT"
            and (positions.get(s) or blank_pos())["side"] != side
            and (contracts.get(s) or {}).get("id")
        ]
        if group_opp:
            await alog(
                f"{symbol} IGNORED — {', '.join(group_opp)} already open opposite in the no-hedge group (no hedging)",
                "warning",
                feed_msg=f"No-hedge skip · {symbol} {direction}",
            )
            return
        if not a_plus and not in_macro_window() and trade_settings.get("macro_time"):
            await alog(f"{symbol} IGNORED — Macro Time: outside the trading window", "warning",
                       feed_msg=f"Macro skip · {symbol} {direction}")
            return
        await alog(f"{symbol} FLAT — opening fresh {direction}", "system",
                   feed_msg=f"Open · {symbol} {direction}")
        await place_order(client, token, account_id, symbol, side, 0, ifvg_info, sl_price, entry_ref, timeframe, c1_high, c1_low, swing_sl, a_plus, a_plus_target)


async def flatten_symbol(symbol: str, reason: str = "flatten", require_side=None) -> bool:
    """Lock-protected: cancel orders + flatten ONE instrument + sweep orphan brackets
    + clear its BE/structural state. Shared by the structural exit, the BE roll-back,
    and the anti-hedge resolver. It re-reads the broker under the lock, so it can never
    close a position that's being (re)opened in the same instant, and `require_side`
    (0=long, 1=short) makes it a no-op unless the live position is on that side."""
    async with trade_lock:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if not token or not account_id:
                await alog(f"{symbol} flatten aborted — auth failed", "error")
                return False
            await refresh_positions(client, token, account_id)
            pos = positions.get(symbol) or blank_pos()
            if pos["direction"] == "FLAT":
                # Nothing to close, but sweep any stranded bracket and clear state.
                await cancel_all_orders(client, token, account_id, symbol)
                structural_pos.pop(symbol, None)
                be_state.pop(symbol, None)
                current_bracket_ids.pop(symbol, None)
                return True
            if require_side is not None and pos["side"] != require_side:
                await alog(
                    f"{symbol} flatten skipped — position is {pos['direction']}, exit was for the other side",
                    "system",
                )
                return False
            # Record WHY we're closing, for outcome classification on the next P&L sync.
            _stamp_open_rows(symbol, exit_reason=reason)
            await alog(
                f"{symbol} FLATTEN | {reason} — closing {pos['size']}× {pos['direction']} at market",
                "warning",
                feed_msg=f"Flatten · {symbol} {pos['direction']} ({reason})",
            )
            await cancel_all_orders(client, token, account_id, symbol)
            ok = await flatten_position(client, token, account_id, symbol, pos["size"], pos["side"])
            if not await confirm_flat(client, token, account_id, symbol):
                await alog(f"{symbol} flatten NOT confirmed — position may still be open; "
                           f"the 30s reconcile/orphan sweep will re-check", "warning")
            await cancel_all_orders(client, token, account_id, symbol)   # sweep orphans now that it's flat
            structural_pos.pop(symbol, None)
            be_state.pop(symbol, None)
            current_bracket_ids.pop(symbol, None)
            return ok


async def handle_structural_exit(symbol: str, reason: str = "IFVG invalidated", exit_tf: str = None):
    """Flatten a structural position when the Pine fires a close-based invalidation
    (a candle CLOSED beyond candle 1's level). Gated three ways so it only ever
    closes the trade it belongs to:
      • structural_pos must be set (the position was opened with Structural SL on),
      • the invalidation timeframe must match the entry timeframe, and
      • the DIRECTION must match — a "bullish" (long) invalidation can only close a
        LONG, a "bearish" (short) one only a SHORT. This stops a stale invalidation
        for an old long from flattening a freshly-flipped short."""
    st = structural_pos.get(symbol)
    if not st:
        await alog(f"EXIT {symbol} ignored — no structural position open", "system")
        return
    entry_tf = (st.get("tf") if isinstance(st, dict) else "") or ""
    if exit_tf and entry_tf and exit_tf != entry_tf:
        await alog(
            f"EXIT {symbol} ignored — {exit_tf} invalidation does not match {entry_tf} entry timeframe",
            "system",
        )
        return
    # Direction gate: bullish-invalidation → close LONG (side 0); bearish → SHORT (1).
    r = (reason or "").lower()
    req_side = 0 if "bullish" in r else (1 if "bearish" in r else None)
    await flatten_symbol(symbol, reason=f"structural exit — {reason}", require_side=req_side)


async def resolve_hedge(prefer_sym: str, prefer_side: int):
    """Real-time anti-hedge backstop. When `prefer_sym` just moved to `prefer_side`,
    flatten any ENABLED same-no-hedge-group instrument sitting on the OPPOSITE side.
    Catches hedges that form OUTSIDE the serialized execute_trade path — e.g. an
    opposite-direction signal whose flatten missed the other leg's still-settling
    fill. Takes the trade lock, so during a legitimate flip it simply waits and then
    finds nothing to do (execute_trade aligns the group itself)."""
    grp = hedge_group(prefer_sym)
    async with trade_lock:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if not token or not account_id:
                return
            await refresh_positions(client, token, account_id)
            victims = [
                s for s in enabled_symbols
                if s in grp and s != prefer_sym
                and (positions.get(s) or blank_pos())["direction"] != "FLAT"
                and (positions.get(s) or blank_pos())["side"] != prefer_side
                and (contracts.get(s) or {}).get("id")
            ]
            if not victims:
                return
            await alog(
                f"ANTI-HEDGE | {prefer_sym} {'LONG' if prefer_side == 0 else 'SHORT'} — flattening opposing {', '.join(victims)}",
                "warning",
                feed_msg=f"Anti-hedge · flatten {', '.join(victims)}",
            )
            for s in victims:
                p = positions.get(s) or blank_pos()
                await cancel_all_orders(client, token, account_id, s)
                await flatten_position(client, token, account_id, s, p["size"], p["side"])
                if not await confirm_flat(client, token, account_id, s):
                    await alog(f"{s} anti-hedge flatten NOT confirmed — will be re-checked next poll", "warning")
                await cancel_all_orders(client, token, account_id, s)
                structural_pos.pop(s, None)
                be_state.pop(s, None)
                current_bracket_ids.pop(s, None)


async def reconcile_open_position_orders(symbol: str):
    """Backstop orphan sweep for an OPEN position (the flat-based reconciler can't
    help here — the contract isn't flat). Used after a DIRECT reversal that may
    have stranded the prior direction's bracket alongside the new one, and as a
    30s poll safety net.

    An order is only cancelled when it is BOTH (a) not in the tracked current
    bracket (current_bracket_ids) and (b) on the WRONG side of the market for the
    live position — so a genuine protective order is never stripped. Race-safe:
    re-reads the position under the lock first; bails if there's no clean price."""
    async with trade_lock:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if not token or not account_id:
                return
            await refresh_positions(client, token, account_id, only_symbol=symbol)
            pos = positions.get(symbol) or blank_pos()
            if pos["direction"] == "FLAT":
                return   # became flat — the flat reconciler / sweep owns this case
            cid = (contracts.get(symbol) or {}).get("id")
            if not cid:
                return
            _q   = quotes.get(symbol) or {}
            _bid, _ask = _q.get("bid"), _q.get("ask")
            mkt  = (_q.get("last") or ((_bid + _ask) / 2 if (_bid is not None and _ask is not None) else (_ask or _bid))
                    or pos.get("avg_price"))
            if mkt is None:
                return   # no reliable market price — don't risk a wrong cancel
            tick   = (contracts.get(symbol) or {}).get("tick") or 0.25
            keep   = current_bracket_ids.get(symbol) or set()
            orders = await search_open_orders(client, token, account_id)
            _ours, orphans = _split_bracket_by_side(orders, cid, pos["side"], float(mkt), tick)
            orphans = [oid for oid in orphans if oid not in keep]
            killed  = await _cancel_ids(client, token, account_id, orphans)
            if killed:
                await alog(
                    f"ORPHAN SWEEP | {symbol} | cancelled {killed} wrong-side order(s) after a reversal",
                    "warning",
                    feed_msg=f"Orphans cleared · {symbol} ({killed})",
                )


def _maybe_resolve_hedge(sym: str, side: int):
    """Called from the live position handler: if `sym` just took `side` and an
    enabled same-group instrument is on the OPPOSITE side, fire the anti-hedge
    resolver. Cheap synchronous check; the heavy lifting runs under the lock."""
    grp = hedge_group(sym)
    for s in enabled_symbols:
        if s == sym or s not in grp:
            continue
        p = positions.get(s) or blank_pos()
        if p["direction"] != "FLAT" and p["side"] != side:
            asyncio.create_task(resolve_hedge(sym, side))
            return


async def reconcile_orphan_orders(symbol: str):
    """Cancel bracket orders (SL/TP) left behind after a position closes — e.g. an
    OCO sibling the broker didn't auto-cancel, or a bracket created late during a
    flip. RACE-SAFE: it takes the trade lock and re-confirms the instrument is FLAT
    from the broker before cancelling, so it can never kill the live brackets of a
    position that is being (re)opened in the same instant."""
    async with trade_lock:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if not token or not account_id:
                return
            size1, _ = await refresh_positions(client, token, account_id, only_symbol=symbol)
            if size1 and size1 > 0:
                return   # still open — those orders are live brackets, leave them
            # Confirm flat with a SECOND read before cancelling. A single flaky/empty
            # position response must never strip the brackets off a live position.
            await asyncio.sleep(0.4)
            size2, _ = await refresh_positions(client, token, account_id, only_symbol=symbol)
            if size2 and size2 > 0:
                return
            await cancel_all_orders(client, token, account_id, symbol)
            current_bracket_ids.pop(symbol, None)


async def sweep_orphans(client, token, account_id):
    """Poll-loop safety net: one account-wide order scan; any instrument that is
    FLAT yet still has open orders is handed to the race-safe reconciler. Catches
    orphans from a missed position-close socket event."""
    try:
        orders = await search_open_orders(client, token, account_id)
        cids_with_orders = {o.get("contractId") for o in orders if isinstance(o, dict)}
        for sym in enabled_symbols:
            cid = (contracts.get(sym) or {}).get("id")
            if cid and cid in cids_with_orders and (positions.get(sym) or blank_pos())["direction"] == "FLAT":
                asyncio.create_task(reconcile_orphan_orders(sym))
    except Exception:
        pass


# ─── SignalR Engine ───────────────────────────────────────────────────────────

async def _ws_send(ws, target: str, args: list):
    msg = json.dumps({"type": 1, "target": target, "arguments": args}) + SIGNALR_SEP
    await ws.send(msg)


async def handle_signalr_msg(msg: dict):
    global account_balance, account_can_trade, session_stats

    if msg.get("type") == 6:   # SignalR ping — ignore
        return
    if msg.get("type") != 1:   # Only process invocations
        return

    target = msg.get("target", "")
    args   = msg.get("arguments", [])
    if not args:
        return
    data = args[0]

    # ── Position update (fires for ALL trades, not just bot orders) ──────────
    if target == "GatewayUserPosition":
        sym = symbol_for_contract(data.get("contractId"))
        if not sym:
            return
        pos_type  = data.get("type", 0)   # 1=Long 2=Short 0=flat
        size      = data.get("size", 0)
        avg_price = data.get("averagePrice")

        if size == 0 or pos_type == 0:
            was_open = (positions.get(sym) or blank_pos())["direction"] != "FLAT"
            positions[sym] = blank_pos()
            # Position closed — clear BE + structural + bracket state so none linger
            be_state.pop(sym, None)
            structural_pos.pop(sym, None)
            current_bracket_ids.pop(sym, None)
            # A close can strand the OCO sibling bracket — sweep it (race-safe).
            if was_open:
                asyncio.create_task(reconcile_orphan_orders(sym))
        else:
            new_side  = 0 if pos_type == 1 else 1
            prev      = positions.get(sym) or blank_pos()
            flipped   = prev["direction"] != "FLAT" and prev["side"] != new_side
            positions[sym] = {
                "size": size, "side": new_side,
                "direction": "LONG" if new_side == 0 else "SHORT", "avg_price": avg_price,
            }
            # A DIRECT flip (long→short with no flat tick in between) can leave the
            # old direction's bracket alive next to the new one — the flat-based
            # reconcile never sees it because the contract isn't flat. Clear stale
            # bracket ids and sweep against the (soon-to-be-recaptured) current set.
            if flipped:
                current_bracket_ids.pop(sym, None)
                asyncio.create_task(reconcile_open_position_orders(sym))
            _maybe_resolve_hedge(sym, new_side)

        await push_state_update()

    # ── Trade fill — TP hit, SL hit, or manual close ─────────────────────────
    elif target == "GatewayUserTrade":
        sym = symbol_for_contract(data.get("contractId"))
        if not sym:
            return
        pnl       = data.get("profitAndLoss")
        fees      = data.get("fees") or 0.0
        price     = data.get("price")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
        side_str  = "BUY" if data.get("side", 0) == 0 else "SELL"

        if pnl is not None:
            net     = round(pnl - fees, 2)
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            net_str = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
            ev_type = "success" if pnl >= 0 else "error"
            await alog(
                f"FILL | {sym} {side_str} @ {price_str} | P&L {pnl_str} | Fees ${fees:.2f} | Net {net_str}",
                ev_type,
            )
            # Back-fill the P&L + classify the outcome on the most recent un-closed
            # trade for THIS instrument, and append it to results.txt.
            for entry in trade_log:
                if (not entry.get("separator") and entry.get("instrument") == sym
                        and entry.get("pnl") == "—"):
                    _finalize_trade_result(entry, net_str)
                    save_state()
                    break
            # Session realized P&L / win-loss is owned by reconcile_session_pnl()
            # (authoritative, sourced from Trade/search). Refresh it now so the
            # dashboard updates the instant a close lands, without waiting for the
            # 30s poll. The old inline tally here was unreliable — this socket
            # event often never fires, and when it did its errors were swallowed.
            asyncio.create_task(_reconcile_session_pnl_now())
        else:
            # Null P&L = opening half-turn (entry fill)
            await alog(f"FILL | {sym} {side_str} @ {price_str} | Entry fill", "system")

        await push_state_update()

    # ── Order status (cancelled / rejected) ──────────────────────────────────
    elif target == "GatewayUserOrder":
        sym      = symbol_for_contract(data.get("contractId")) or ""
        status   = data.get("status")
        oid      = data.get("id")
        side_str = "BUY" if data.get("side", 0) == 0 else "SELL"
        tag      = f"{sym} " if sym else ""
        if status == 3:
            await alog(f"ORDER CANCELLED | {tag}{side_str} | ID: {oid}", "warning")
        elif status == 5:
            await alog(f"ORDER REJECTED  | {tag}{side_str} | ID: {oid}", "error")

    # ── Account balance + trading permission ─────────────────────────────────
    elif target == "GatewayUserAccount":
        balance   = data.get("balance")
        can_trade = data.get("canTrade", True)
        if balance is not None:
            account_balance = round(float(balance), 2)
        account_can_trade = can_trade
        if not can_trade:
            await alog(
                f"TRADING RESTRICTED — canTrade=false | Balance: ${account_balance:.2f}",
                "error",
            )
        await push_state_update()


# A short-lived token cache. TopstepX tokens expire (the websockets reconnect
# with a FRESH token every time, but the poll loop reuses a cached one so it
# isn't hammering the auth endpoint). TTL is kept well under the token lifetime.
_auth_cache = {"token": None, "account_id": None, "ts": 0.0}
_AUTH_TTL   = 600   # seconds (10 min)


async def get_auth(client, force: bool = False):
    """Return (token, account_id), reusing a cached token unless it's stale
    or `force` is set. Forcing happens on every websocket (re)connect so a
    dropped/expired token never gets retried forever."""
    now = time.monotonic()
    if (not force and _auth_cache["token"]
            and now - _auth_cache["ts"] < _AUTH_TTL):
        return _auth_cache["token"], _auth_cache["account_id"]
    token, account_id = await get_token_and_account(client)
    if token and account_id:
        _auth_cache.update(token=token, account_id=account_id, ts=now)
    return token, account_id


async def start_signalr_user_hub():
    """Live position / fills / orders / balance. Re-auths on every (re)connect
    so an expired token can't wedge the stream permanently."""
    global signalr_user_connected, _user_ws
    while True:
        try:
            async with api_client() as client:
                token, account_id = await get_auth(client, force=True)
            if not token or not account_id:
                signalr_user_connected = False
                await push_state_update()
                log("SignalR user hub — auth failed, retry in 10s", "warning")
                await asyncio.sleep(10)
                continue
            url = f"{SIGNALR_USER_HUB}?access_token={token}"
            async with websockets.connect(url, max_size=2**20, open_timeout=15, ssl=_SSL_CTX) as ws:
                _user_ws = ws
                await ws.send(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_SEP)
                await ws.recv()                             # handshake ACK
                await _ws_send(ws, "SubscribeAccounts",  [])
                await _ws_send(ws, "SubscribePositions", [account_id])
                await _ws_send(ws, "SubscribeOrders",    [account_id])
                await _ws_send(ws, "SubscribeTrades",    [account_id])
                signalr_user_connected = True
                log("SignalR user hub CONNECTED — live position / fills / balance active")
                await push_state_update()
                async for raw in ws:
                    for part in raw.split(SIGNALR_SEP):
                        if not part.strip():
                            continue
                        try:
                            msg = json.loads(part)
                        except Exception:
                            continue   # non-JSON keepalive frame — ignore quietly
                        try:
                            await handle_signalr_msg(msg)
                        except Exception as e:
                            log(f"SignalR user msg handler error: {e}", "warning")
        except Exception as e:
            signalr_user_connected = False
            await push_state_update()
            log(f"SignalR user hub disconnected ({e}) — retry in 5s", "warning")
            await asyncio.sleep(5)
        finally:
            _user_ws = None


async def start_signalr_market_hub():
    """Live quotes for EVERY enabled instrument. Re-auths and re-resolves
    contracts on every (re)connect, so toggling instruments (which closes this
    socket) reconnects subscribed to the new set. Quotes route by contractId."""
    global signalr_market_connected, _market_ws
    while True:
        try:
            async with api_client() as client:
                token, _ = await get_auth(client, force=True)
                if token:
                    await ensure_contracts(client, token)
            cids = [(contracts.get(s) or {}).get("id") for s in enabled_symbols]
            cids = [c for c in cids if c]
            if not token or not cids:
                signalr_market_connected = False
                log("SignalR market hub — auth/contracts not ready, retry in 10s", "warning")
                await asyncio.sleep(10)
                continue
            url = f"{SIGNALR_MARKET_HUB}?access_token={token}"
            async with websockets.connect(url, max_size=2**20, open_timeout=15, ssl=_SSL_CTX) as ws:
                _market_ws = ws
                await ws.send(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_SEP)
                await ws.recv()
                for cid in cids:
                    await _ws_send(ws, "SubscribeContractQuotes", [cid])
                signalr_market_connected = True
                log(f"SignalR market hub CONNECTED — live quotes for {', '.join(enabled_symbols)}")
                await push_state_update()
                async for raw in ws:
                    for part in raw.split(SIGNALR_SEP):
                        if not part.strip():
                            continue
                        try:
                            msg = json.loads(part)
                            if msg.get("type") == 1 and msg.get("target") == "GatewayQuote":
                                a   = msg.get("arguments", [])
                                # Event is (contractId, data); be tolerant of a single-arg form.
                                if len(a) >= 2 and isinstance(a[1], dict):
                                    cid, d = a[0], a[1]
                                else:
                                    d   = a[0] if a else {}
                                    cid = d.get("contractId")
                                sym = symbol_for_contract(cid)
                                if not sym:
                                    continue
                                # MERGE, don't overwrite: TopstepX sends PARTIAL
                                # quote updates (one event may carry only bestAsk,
                                # the next only lastPrice). Rebuilding the dict each
                                # event wiped the absent fields to None — which left
                                # `last` None whenever the latest tick lacked it,
                                # breaking break-even (needs last) and stacked-entry
                                # SL/TP inheritance (needs a price ref). Only update
                                # fields actually present in this event.
                                q = quotes.get(sym) or blank_quote()
                                for _out, _in in (("last", "lastPrice"), ("bid", "bestBid"),
                                                  ("ask", "bestAsk"), ("change", "change"),
                                                  ("change_pct", "changePercent"),
                                                  ("high", "high"), ("low", "low")):
                                    _v = d.get(_in)
                                    if _v is not None:
                                        q[_out] = _v
                                quotes[sym] = q
                        except Exception:
                            pass
        except Exception as e:
            signalr_market_connected = False
            log(f"SignalR market hub disconnected ({e}) — retry in 5s")
            await asyncio.sleep(5)
        finally:
            _market_ws = None


def _backfill_trade_log_pnl(closes_by_sym: dict, session: str = None, notify: bool = True) -> bool:
    """Persist P&L onto trade_log rows still showing '—', using the broker's
    authoritative closed-trade list. This is the reliable path: the
    GatewayUserTrade socket event is best-effort and often never fires, so rows
    used to be saved as '—' forever and every past session showed no P&L (EFF-6).
    Because reconcile runs every poll + on each fill, by shutdown the session's
    log is fully populated — so past sessions become reviewable in the dashboard.

    Per instrument the k-th close (chronological) maps to the k-th entry
    (chronological) for that instrument WITHIN one session. Scoping to a session is
    essential: `closes_by_sym` only holds the queried session's closes, so without
    the session filter this session's P&L would mis-map onto an EARLIER session's
    still-'—' rows (the oldest rows for that symbol). `session` defaults to the
    current 6 PM-NY session (reconcile's domain); the startup history backfill passes
    each past session explicitly. Only '—' rows are written, so it's idempotent and
    never overwrites a value that's already known. Returns True if anything changed."""
    sess = session or _ny_session_id()
    changed = False
    for sym, nets in closes_by_sym.items():
        # trade_log is newest-first (appendleft) → reverse to walk oldest-first.
        entries = [e for e in reversed(trade_log)
                   if not e.get("separator") and e.get("instrument") == sym
                   and e.get("session") == sess]
        for entry, net_str in zip(entries, nets):
            if entry.get("pnl") == "—":
                if _finalize_trade_result(entry, net_str, notify=notify):
                    changed = True
    if changed:
        save_state()
    return changed


async def _backfill_session_pnl_history():
    """One-shot at startup: finalize P&L + outcome on trade_log rows whose session
    reconcile_session_pnl will never cover — every PAST 6 PM-NY session, plus the part
    of the CURRENT session that ran before this restart (reconcile only sees closes
    since the process started, so a restart otherwise freezes those rows at '—' and
    the Results tab shows blanks for all history).

    For each session that still has unfinalized, date-tagged rows it queries that 6 PM-NY
    day's closed trades from the broker and reuses the same session-scoped per-instrument
    mapping. session_stats is deliberately left to reconcile, so a past session's P&L
    never inflates THIS session's totals; A+ result pushes are suppressed so old closes
    don't re-alert. Rows with no `session` (legacy schema, undated) can't be queried and
    are left as '—'. Must run after ensure_contracts (symbol_for_contract needs the map)."""
    pending_rows = [e for e in trade_log
                    if not e.get("separator") and e.get("pnl") == "—" and e.get("session")]
    pending = sorted({e.get("session") for e in pending_rows})
    if not pending:
        return
    try:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if not (token and account_id):
                return
            for sess in pending:
                try:
                    open_ny = datetime.strptime(sess, "%Y-%m-%d").replace(hour=18, tzinfo=NY_TZ)
                except Exception:
                    continue   # malformed session id → skip
                try:
                    res = await client.post(
                        "https://api.topstepx.com/api/Trade/search",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "accountId":      account_id,
                            "startTimestamp": open_ny.astimezone(timezone.utc).isoformat(),
                            "endTimestamp":   (open_ny + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                        },
                    )
                    if res.status_code != 200:
                        continue
                    trades = res.json().get("trades", []) or []
                except Exception:
                    continue
                trades = sorted(trades, key=lambda t: t.get("creationTimestamp") or "")
                closes_by_sym = {}
                for t in trades:
                    if t.get("voided"):
                        continue
                    pnl = t.get("profitAndLoss")
                    if pnl is None:                       # opening half-turn → no realized P&L
                        continue
                    sym = symbol_for_contract(t.get("contractId"))
                    if not sym:                           # rolled/expired contract → can't attribute
                        continue
                    net = round(pnl - (t.get("fees") or 0.0), 2)
                    closes_by_sym.setdefault(sym, []).append(
                        f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}")
                _backfill_trade_log_pnl(closes_by_sym, session=sess, notify=False)
                await asyncio.sleep(0.2)                  # be gentle on the rate budget
    except Exception as e:
        log(f"Session P&L history backfill error: {e}", "warning")
        return
    filled = sum(1 for e in pending_rows if e.get("pnl") != "—")
    if filled:
        log(f"Backfilled P&L on {filled} trade(s) across {len(pending)} prior session(s)", "system")
        await push_state_update()


def _loss_streak(nets) -> int:
    """Length of the trailing run of consecutive losses at the end of `nets`
    (a chronological list of net P&L per closed trade). A non-loss breaks it."""
    n = 0
    for net in reversed(nets):
        if net < 0:
            n += 1
        else:
            break
    return n


async def _breaker_auto_resume():
    """Lift the circuit-breaker pause after the cooldown — but only if the breaker
    is still what's holding it (a manual pause/resume in the meantime wins)."""
    global bot_paused
    await asyncio.sleep(CIRCUIT_BREAKER_COOLDOWN)
    if not _breaker["active"]:
        return                      # already cleared by a manual resume
    _breaker["active"] = False
    _breaker["until"]  = 0.0
    if bot_paused:
        bot_paused = False
        await alog("✅ CIRCUIT BREAKER lifted — cooldown over, trading resumed automatically.",
                   "system", feed_msg="Circuit breaker · trading resumed")
    await push_state_update()


async def _evaluate_circuit_breaker(nets):
    """Trip the breaker when the fresh tail of closed trades shows
    CIRCUIT_BREAKER_LOSSES consecutive losses. `nets` is the chronological list of
    net P&L per realized close this session. Only trades not already consumed by a
    prior trip are considered, so the same losing run can't re-trip after resume."""
    global bot_paused
    if _breaker["active"]:
        return                                      # already in a cooldown
    fresh = nets[_breaker["handled_through"]:]
    if _loss_streak(fresh) < CIRCUIT_BREAKER_LOSSES:
        return
    # Trip it. Consume every close seen so far so this run never re-trips, pause
    # all instruments, and count the trip against the session's daily cap.
    _breaker["handled_through"] = len(nets)
    _breaker["trips"] += 1
    _breaker["active"] = True
    bot_paused = True
    mins = CIRCUIT_BREAKER_COOLDOWN // 60

    # Trip → ALWAYS auto-resume after the cooldown. (The old daily HARD halt —
    # indefinite pause requiring a manual resume — was removed by design: the bot
    # must never stop trading on its own beyond this fixed cooldown.)
    _breaker["until"] = time.time() + CIRCUIT_BREAKER_COOLDOWN
    await alog(
        f"🛑 CIRCUIT BREAKER — {CIRCUIT_BREAKER_LOSSES} losing trades in a row "
        f"(trip {_breaker['trips']} this session). All trading paused for {mins} min, "
        f"then auto-resumes.",
        "error",
        feed_msg=f"Circuit breaker · paused {mins}m (trip {_breaker['trips']})",
    )
    asyncio.create_task(notify_push(
        f"🛑 KAIROS circuit breaker — {CIRCUIT_BREAKER_LOSSES} losses in a row. "
        f"Paused {mins}m (trip {_breaker['trips']}), then auto-resumes.", mention=True,
        embed=_embed("🛑 Circuit Breaker — paused", "warn",
                     fields=[("Reason", f"{CIRCUIT_BREAKER_LOSSES} losses in a row"),
                             ("Paused", f"{mins} min"), ("Trip", str(_breaker['trips']))],
                     footer=f"auto-resumes · {now_ny()} NY")))
    asyncio.create_task(_breaker_auto_resume())
    await push_state_update()


async def reconcile_session_pnl(client, token, account_id):
    """Authoritative session realized-P&L + win/loss, computed from the broker's
    own record of closed trades since the session began (Trade/search).

    Recomputed from scratch on every call, so it's idempotent — safe to run on
    each poll and on each fill without ever double-counting. This is the RELIABLE
    source: the GatewayUserTrade socket event is best-effort and in practice
    often never arrives, which is why realized_pnl/wins/losses used to sit at 0
    even after losing trades had clearly closed."""
    try:
        res = await client.post(
            "https://api.topstepx.com/api/Trade/search",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "accountId":      account_id,
                "startTimestamp": _SESSION_START_UTC.isoformat(),
                "endTimestamp":   datetime.now(timezone.utc).isoformat(),
            },
        )
        if res.status_code != 200:
            return
        trades = res.json().get("trades", []) or []
    except Exception:
        return   # never let a stats refresh break the poll / fill handler

    # Walk closes in the order the broker created them, so the loss-streak and the
    # per-instrument P&L backfill are both chronological. Missing timestamp → the
    # stable sort keeps the broker's own ordering.
    trades = sorted(trades, key=lambda t: t.get("creationTimestamp") or "")

    realized = 0.0
    wins = losses = 0
    nets = []            # chronological net P&L per realized close (streak + breaker)
    closes_by_sym = {}   # sym -> [net_str, ...] chronological (trade_log backfill)
    _adaptive_events = []   # ladder transitions to notify about after the loop
    for t in trades:
        if t.get("voided"):
            continue
        pnl = t.get("profitAndLoss")
        if pnl is None:            # opening half-turn carries no realized P&L
            continue
        net = round(pnl - (t.get("fees") or 0.0), 2)
        realized += net
        nets.append(net)
        # Adaptive ladder: advance the multiplier / daily-stop for each NEW close.
        # Guarded by the persisted last_ts so a re-run of this idempotent reconcile
        # (every poll/fill, and after restarts) never applies the same trade twice.
        if adaptive_enabled:
            _ts = t.get("creationTimestamp") or ""
            if _ts and _ts > adaptive_state.get("last_ts", ""):
                _ev = adaptive.apply_close(adaptive_state, adaptive_settings, net, _futures_day_key())
                adaptive_state["last_ts"] = _ts
                _adaptive_events.append(_ev)
        if net >= 0: wins += 1
        else:        losses += 1
        sym = symbol_for_contract(t.get("contractId"))
        if sym:
            net_str = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
            closes_by_sym.setdefault(sym, []).append(net_str)

    session_stats["realized_pnl"] = round(realized, 2)
    session_stats["wins"]   = wins
    session_stats["losses"] = losses

    # Adaptive ladder side-effects: log cuts, announce the daily-loss stop, and
    # persist the advanced ladder so the multiplier carries across restarts/days.
    if adaptive_enabled and _adaptive_events:
        for _ev in _adaptive_events:
            if _ev.get("cut"):
                await alog(f"⬇️ Adaptive risk cut — multiplier {_ev['mult_before']:.2f} → "
                           f"{_ev['mult_after']:.2f} ({adaptive_settings.get('cut_after', 2)} losses in a row)",
                           "warning", feed_msg="Adaptive · risk cut")
            if _ev.get("stop_tripped"):
                await alog(f"🛑 Adaptive DAILY-LOSS STOP — {adaptive_settings.get('daily_loss_limit', 10)} "
                           f"losses today. Signals ignored until you resume manually.",
                           "error", feed_msg="Adaptive · daily stop (manual resume)")
                asyncio.create_task(notify_push(
                    f"🛑 KAIROS adaptive daily-loss stop — {adaptive_settings.get('daily_loss_limit', 10)} "
                    f"losses today. Trading halted, manual resume required.", mention=True))
        save_state()

    _backfill_trade_log_pnl(closes_by_sym)     # C1 — persist P&L onto '—' rows
    await _evaluate_circuit_breaker(nets)      # B1 — auto-pause on a losing run


async def _reconcile_session_pnl_now():
    """One-shot reconcile + dashboard push — updates session P&L the instant a
    closing fill is seen, instead of waiting for the next poll cycle."""
    try:
        async with api_client() as client:
            token, account_id = await get_auth(client)
            if token and account_id:
                await reconcile_session_pnl(client, token, account_id)
        await push_state_update()
    except Exception:
        pass


async def be_monitor_loop():
    """Check every second whether any open position has reached the 50%-of-TP
    profit threshold and, if so, move its stop loss to break even (avg_price
    ± 1 tick). Uses the live SignalR quote stream — no extra API calls for price.

    Also handles the case where we placed an entry but didn't capture any SL
    order IDs yet (e.g. broker was slow): retries find_sl_orders() on those."""
    await asyncio.sleep(5)   # let SignalR connect and initial quotes arrive
    _last_tp_poll = 0.0
    while True:
        await asyncio.sleep(1)
        try:
            # Pull ONE open-orders snapshot every TP_RECHECK_SECS so a MANUAL
            # take-profit move re-bases the BE trigger to 50% of the NEW target.
            # One snapshot is shared across all armed symbols (no per-symbol HTTP).
            _orders_snap = None
            _now = time.time()
            if be_state and (_now - _last_tp_poll) >= TP_RECHECK_SECS:
                _last_tp_poll = _now
                try:
                    async with api_client() as client:
                        token, account_id = await get_auth(client)
                        if token and account_id:
                            _orders_snap = await search_open_orders(client, token, account_id)
                except Exception:
                    _orders_snap = None

            for symbol in list(be_state.keys()):
                state = be_state.get(symbol)
                if not state:
                    continue
                if state.get("be_triggered"):
                    continue
                pos = positions.get(symbol) or {}
                if pos.get("direction") == "FLAT":
                    continue

                avg  = pos.get("avg_price")
                _q   = quotes.get(symbol) or {}
                _bid, _ask = _q.get("bid"), _q.get("ask")
                last = _q.get("last") or ((_bid + _ask) / 2 if (_bid is not None and _ask is not None) else (_ask or _bid))
                if avg is None or last is None:
                    continue

                # ── Re-base the BE trigger off the LIVE take-profit ────────────
                # The 50% trigger follows the current TP limit order, so if the
                # target is moved manually the break-even point moves with it
                # (BE now fires at 50% of the new entry→TP distance). We track the
                # live TP price as the baseline: the first read after an entry
                # silently adopts the exact TP (more accurate than the entry-time
                # estimate); only a SUBSEQUENT change in that price is a manual move
                # worth announcing.
                if _orders_snap is not None:
                    _sl_live, _tp_live = await fetch_bracket_prices(
                        None, None, None, symbol, orders=_orders_snap)
                    if _tp_live is not None and avg is not None:
                        _tick     = (contracts.get(symbol) or {}).get("tick") or 0.25
                        _prev_live = state.get("live_tp_price")
                        _new_tp_pts = round(abs(_tp_live - avg), 4)
                        if _prev_live is None:
                            # First live read this position — adopt silently.
                            state["live_tp_price"] = _tp_live
                            if _new_tp_pts > 0:
                                state["tp_pts"]        = _new_tp_pts
                                state["orig_tp_price"] = _tp_live
                        elif abs(_tp_live - _prev_live) > (_tick / 2) and _new_tp_pts > 0:
                            # TP limit was moved manually → re-base and announce.
                            _old_tp = state.get("tp_pts", 0.0)
                            state["live_tp_price"] = _tp_live
                            state["tp_pts"]        = _new_tp_pts
                            state["orig_tp_price"] = _tp_live
                            await alog(
                                f"BE re-based | {symbol} | TP moved to {_tp_live} ({_new_tp_pts}pt) → "
                                f"BE now triggers at {round(_new_tp_pts / 2, 2)}pt profit "
                                f"(was {round(_old_tp / 2, 2)}pt)",
                                "system",
                                feed_msg=f"BE re-based · {symbol}",
                            )

                # Unrealized profit in points
                if pos["direction"] == "LONG":
                    unrealized_pts = last - avg
                else:
                    unrealized_pts = avg - last

                tp_pts  = state.get("tp_pts", 0.0)
                trigger = tp_pts / 2.0
                if trigger <= 0:
                    continue

                # If SL order IDs are still missing, recover them from the SAME
                # snapshot — no extra API call. This only runs on a polling tick
                # (snapshot present); on other ticks we just wait. Crucially this
                # avoids a per-SECOND find_sl_orders poll for a position that has no
                # stop to find (e.g. structural phase 2), which would otherwise burn
                # ~60 requests/min. If there genuinely is no stop, trigger_breakeven
                # places a fresh break-even one once the threshold is hit.
                if not state.get("sl_order_ids") and unrealized_pts < trigger:
                    if _orders_snap is not None:
                        try:
                            ids = await find_sl_orders(None, None, None, symbol, orders=_orders_snap)
                            if ids:
                                state["sl_order_ids"] = ids
                                await alog(
                                    f"BE monitor | {symbol} | SL order(s) recovered: {ids}",
                                    "system",
                                )
                        except Exception:
                            pass
                    continue   # wait for next tick to re-evaluate with IDs populated

                if unrealized_pts >= trigger:
                    try:
                        async with api_client() as client:
                            token, account_id = await get_auth(client)
                            if token and account_id:
                                await trigger_breakeven(client, token, account_id, symbol)
                    except Exception as e:
                        await alog(f"BE monitor error for {symbol}: {e}", "warning")
        except Exception:
            pass


async def structural_monitor_loop():
    """Self-contained close-based structural exit. For every structural position that
    carries a stored IFVG invalidation level (candle-1 HIGH for a long, candle-1 LOW
    for a short — the same level the phase-1 hard stop sat at), this watches the live
    quote stream and, at each close of a candle on the ENTRY timeframe, flattens if
    that candle CLOSED beyond the level (long → close below the level; short → close
    above it). Wicks through the level are allowed — only closes count.

    Why this exists: the structural exit used to rely solely on a separate
    TradingView 'exit' alert, which in practice never fired — so structural trades
    silently ran all the way to the wide 20/40pt safety net. This makes the bot
    enforce the close-based stop itself. The broker safety net stays as the ultimate
    backstop; the per-signal TV exit alert still works too (whichever trips first).

    Guards against premature exits: needs a numeric level + an intraday timeframe;
    never evaluates the entry candle (only a SUBSEQUENT close counts); requires the
    close to be at least half a tick beyond the level; and flatten_symbol re-reads
    the live position under the lock and is direction-gated, so a flipped/closed
    position is never touched."""
    await asyncio.sleep(7)   # let SignalR connect and quotes arrive
    last_eval_bucket: dict = {}   # symbol -> last candle bucket already evaluated
    while True:
        await asyncio.sleep(1)
        try:
            now = time.time()
            for symbol in list(structural_pos.keys()):
                st = structural_pos.get(symbol)
                if not isinstance(st, dict) or st.get("exiting"):
                    continue
                level = st.get("level")
                side  = st.get("side")
                tf    = st.get("tf") or ""
                if level is None or side is None:
                    continue   # no stored level → alert-only fallback for this trade

                # Current price (each tick) — used for the close-based exit below.
                _q   = quotes.get(symbol) or {}
                _bid, _ask = _q.get("bid"), _q.get("ask")
                last = _q.get("last") or ((_bid + _ask) / 2 if (_bid is not None and _ask is not None) else (_ask or _bid))

                bucket = _candle_bucket(tf, now)
                if bucket is None:
                    continue   # non-intraday timeframe → can't self-monitor
                entry_bucket = st.get("entry_bucket")
                baseline = last_eval_bucket.get(symbol)
                if baseline is None:
                    baseline = entry_bucket if entry_bucket is not None else bucket
                if bucket <= baseline:
                    continue   # still inside a candle we've already accounted for
                # A candle boundary was crossed → the prior candle just closed.
                last_eval_bucket[symbol] = bucket

                pos = positions.get(symbol) or {}
                if pos.get("direction") == "FLAT" or pos.get("side") != side:
                    continue   # position gone or flipped — not ours to exit

                # ── Phase 1 → 2: the entry candle just closed ────────────────────
                # STRUCTURAL → remove the candle-1 hard stop and hand over to the
                #           close-based exit below (unless auto-BE already set BE).
                if st.get("two_phase") and st.get("phase") == 1:
                    st["phase"] = 2
                    _stamp_open_rows(symbol, reached_phase2=True)   # survived entry candle
                    _be_fired = bool((be_state.get(symbol) or {}).get("be_triggered"))
                    # Remove the candle-1 hard stop → close-based exit, unless auto-BE
                    # already set break-even inside the entry candle.
                    if not _be_fired:
                        try:
                            async with api_client() as client:
                                token, account_id = await get_auth(client)
                                if token and account_id:
                                    sl_ids = await find_sl_orders(client, token, account_id, symbol)
                                    if sl_ids:
                                        await _cancel_ids(client, token, account_id, sl_ids)
                                        await alog(
                                            f"STRUCTURAL | {symbol} | candle-1 hard stop removed — "
                                            f"now close-based (phase 2)",
                                            "system",
                                            feed_msg=f"Struct phase 2 · {symbol}",
                                        )
                        except Exception:
                            pass

                if last is None:
                    continue
                tick = (contracts.get(symbol) or {}).get("tick") or 0.25
                buf  = tick / 2.0
                long_invalid  = side == 0 and last < (level - buf)
                short_invalid = side == 1 and last > (level + buf)
                if long_invalid or short_invalid:
                    st["exiting"] = True   # guard against a double-fire next tick
                    await alog(
                        f"STRUCTURAL EXIT | {symbol} | {tf} candle closed "
                        f"{'below' if side == 0 else 'above'} level {level} "
                        f"(last {last}) — flattening",
                        "warning",
                        feed_msg=f"Structural exit · {symbol}",
                    )
                    asyncio.create_task(flatten_symbol(
                        symbol, reason="structural close beyond level", require_side=side))
                    last_eval_bucket.pop(symbol, None)
        except Exception:
            pass


async def position_poll_loop():
    """Fallback position reconciliation. SignalR delivers position changes in
    real time; this only re-checks every POLL_INTERVAL seconds as a safety net
    in case a websocket message was missed or the socket is down."""
    await asyncio.sleep(20)   # initial delay to let SignalR connect first
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with api_client() as client:
                token, account_id = await get_auth(client)
                if token and account_id:
                    await ensure_contracts(client, token)
                    await refresh_positions(client, token, account_id)
                    await sweep_orphans(client, token, account_id)
                    await reconcile_session_pnl(client, token, account_id)
            _scan_and_resolve_hedges()   # fallback: catch any hedge a missed event left behind
            # Backstop for reversal-orphans on OPEN positions (a missed flip event):
            # the flat-based sweep_orphans can't see these. reconcile_open_position_orders
            # only cancels wrong-side, untracked orders, so it's a no-op when clean.
            for sym in list(enabled_symbols):
                if (positions.get(sym) or blank_pos())["direction"] != "FLAT":
                    asyncio.create_task(reconcile_open_position_orders(sym))
            await push_state_update()
        except Exception:
            pass


def _scan_and_resolve_hedges():
    """Fallback hedge sweep (poll-driven). The event-driven resolver catches hedges
    within ~1s; this is the safety net for a missed position event. For any no-hedge
    group holding both sides, keep the MAJORITY-size side and flatten the rest."""
    for grp in NO_HEDGE_GROUPS:
        members = [s for s in enabled_symbols
                   if s in grp and (positions.get(s) or blank_pos())["direction"] != "FLAT"]
        sides = {(positions.get(s) or blank_pos())["side"] for s in members}
        if len(sides) < 2:
            continue   # aligned or flat — no hedge
        long_sz  = sum((positions.get(s) or blank_pos())["size"] for s in members
                       if (positions.get(s) or blank_pos())["side"] == 0)
        short_sz = sum((positions.get(s) or blank_pos())["size"] for s in members
                       if (positions.get(s) or blank_pos())["side"] == 1)
        keep_side = 0 if long_sz >= short_sz else 1
        keep_sym  = next((s for s in members if (positions.get(s) or blank_pos())["side"] == keep_side), None)
        if keep_sym:
            asyncio.create_task(resolve_hedge(keep_sym, keep_side))


async def signalr_startup():
    """Resolve all enabled contracts once, then launch all real-time tasks. Each
    task self-authenticates and re-auths on reconnect, so there's no single token
    that can go stale and kill everything."""
    await asyncio.sleep(3)
    try:
        async with api_client() as client:
            token, account_id = await get_auth(client, force=True)
            if token:
                await ensure_contracts(client, token, force=True)
                if account_id:
                    await reconcile_session_pnl(client, token, account_id)
                    await refresh_positions(client, token, account_id)
                    # ── Arm BE state for positions already open at startup ─────
                    # This covers trades that were running before the bot restarted.
                    # We can't recover the original TP, so we use the current TP
                    # setting for that instrument as the trigger reference.
                    if trade_settings.get("auto_be"):
                        for sym in enabled_symbols:
                            pos = positions.get(sym) or {}
                            if pos.get("direction") in ("LONG", "SHORT"):
                                sl_ids = await find_sl_orders(client, token, account_id, sym)
                                if sl_ids:
                                    tp_ref = tp_pts_for(sym)
                                    be_state[sym] = {
                                        "sl_order_ids": sl_ids,
                                        "be_triggered": False,
                                        "tp_pts":       tp_ref,
                                        "mode":         trading_mode,
                                    }
                                    log(
                                        f"[BE] Startup: armed for existing {sym} {pos['direction']} "
                                        f"| trigger at {round(tp_ref / 2, 2)}pt | SL orders: {sl_ids}"
                                    )
    except Exception as e:
        log(f"Startup contract resolve failed: {e}", "warning")
    asyncio.create_task(start_signalr_user_hub())
    asyncio.create_task(start_signalr_market_hub())
    asyncio.create_task(position_poll_loop())
    asyncio.create_task(be_monitor_loop())
    asyncio.create_task(structural_monitor_loop())
    # Finalize P&L on rows from prior sessions (and the pre-restart part of this
    # session) that reconcile_session_pnl can't reach — runs after the contract map
    # above is built, so symbol_for_contract() can attribute each broker close.
    asyncio.create_task(_backfill_session_pnl_history())


@app.on_event("startup")
async def on_startup():
    _init_results_session()   # seed the results.txt session divider tracker
    asyncio.create_task(signalr_startup())
    asyncio.create_task(state_writer_loop())
    asyncio.create_task(heartbeat_loop())
    # "KAIROS online" ping so you know the service came up (and when).
    asyncio.create_task(notify_push(
        f"🟢 KAIROS online @ {now_ny()} NY ({now_ist('%I:%M %p')} IST)",
        embed=_embed("🟢 KAIROS online", "info",
                     fields=[("NY", now_ny()), ("IST", now_ist('%I:%M %p'))],
                     footer="Trading service started")))


@app.on_event("shutdown")
async def on_shutdown():
    # Flush any state the debounced writer hasn't persisted yet, then release
    # the pooled HTTP client's connections.
    if _state_dirty:
        try:
            _write_state_file(_serialize_state())
        except Exception as e:
            print(f"[STATE] Final save failed: {e}")
    try:
        await HTTP.aclose()
    except Exception:
        pass



# ─── Auth ─────────────────────────────────────────────────────────────────────
async def require_dashboard_auth(
    x_dashboard_token: str = Header(None),
    token: str = Query(None),
    dash: str = Cookie(None),
):
    """Gate the dashboard + control/read endpoints behind DASHBOARD_TOKEN.
    Accepts the token via the X-Dashboard-Token header (fetch calls), an
    HttpOnly `dash` cookie (page load + SSE — keeps the credential out of
    URLs/browser history/ngrok request logs), or a `token` query param
    (first visit / backward compat — the page scrubs it from the URL)."""
    if not DASHBOARD_TOKEN:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN not configured")
    provided = x_dashboard_token or token or dash
    if not provided or not hmac.compare_digest(provided, DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Routes ───────────────────────────────────────────────────────────────────
# Replay / duplicate guard for webhook alerts. The webhook is authenticated by a
# static shared secret, so a captured alert payload could be replayed verbatim to
# spam orders; TradingView can also double-fire the same alert. Any actionable
# signal identical in (symbol, action, timeframe) to one seen within the window
# is dropped. Window must stay below the strategy's minimum re-signal interval.
_recent_signals: dict = {}     # (symbol, action, timeframe) -> time.monotonic()
_DEDUPE_WINDOW = 3.0           # seconds


@app.post("/webhook")
async def receive_webhook(request: Request):
    global session_stats

    # ── Authenticate the alert BEFORE doing anything that can place a trade ──
    if not WEBHOOK_SECRET:
        await alog("Webhook REJECTED — WEBHOOK_SECRET not configured", "error")
        raise HTTPException(status_code=503, detail="Webhook auth not configured")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    provided_secret = payload.get("secret") or request.headers.get("X-Webhook-Secret")
    if not provided_secret or not hmac.compare_digest(str(provided_secret), WEBHOOK_SECRET):
        await alog("Webhook REJECTED — bad/missing secret", "warning")
        raise HTTPException(status_code=401, detail="Unauthorized")

    action     = payload.get("action", "").lower()
    timeframe  = payload.get("timeframe", "")
    ifvg_type  = payload.get("ifvg_type", "")
    sig_symbol = (payload.get("symbol") or "").upper().strip()

    # ── Replay / duplicate guard (see _recent_signals above) ──────────────────
    if action in ("buy", "sell", "exit", "flatten", "close"):
        _key  = (sig_symbol, action, timeframe)
        _mono = time.monotonic()
        _seen = _recent_signals.get(_key)
        if _seen is not None and _mono - _seen < _DEDUPE_WINDOW:
            await alog(
                f"DUPLICATE — {action.upper()} {sig_symbol or '—'} ({timeframe or 'no tf'}) "
                f"repeated within {_DEDUPE_WINDOW:.0f}s — ignored",
                "warning",
                feed_msg=f"Duplicate · {sig_symbol or '—'} {action.upper()}",
            )
            return {"status": "ignored — duplicate signal"}
        _recent_signals[_key] = _mono
        if len(_recent_signals) > 64:   # tiny key space, but never let it grow unbounded
            for k in [k for k, t in _recent_signals.items() if _mono - t > 60]:
                _recent_signals.pop(k, None)

    # ── Structural invalidation exit (Pine: a candle CLOSED beyond candle 1) ──
    # Close-based stop — flatten the instrument if its position is structural.
    if action in ("exit", "flatten", "close"):
        sym = sig_symbol or (enabled_symbols[0] if len(enabled_symbols) == 1 else "")
        reason = payload.get("reason") or "IFVG invalidated (candle closed beyond candle 1)"
        await alog(f"EXIT SIGNAL | {sym or '—'} | {reason}", "warning")
        if sym in INSTRUMENTS:
            asyncio.create_task(handle_structural_exit(sym, reason, exit_tf=timeframe))
            return {"status": f"{sym} exit received"}
        return {"status": "ignored — exit could not resolve a symbol"}

    # Structural SL fields from Pine Script. sl_price = candle-1 close-invalidation
    # level (high[1] for bullish, low[1] for bearish). c1_high/c1_low = candle-1's
    # full range, needed for the two-phase hard stop (see place_order). When the
    # alert omits c1_high/c1_low the bot falls back to the single-phase safety net.
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None
    sl_price  = _f(payload.get("sl_price"))
    entry_ref = _f(payload.get("entry_ref"))
    c1_high   = _f(payload.get("c1_high"))
    c1_low    = _f(payload.get("c1_low"))
    swing_sl  = _f(payload.get("swing_sl"))    # 15-bar low (long) / high (short), from Pine
    imbalance = _f(payload.get("imbalance"))   # inverted-FVG gap size in TICKS (from Pine)
    a_plus    = bool(payload.get("a_plus"))     # A+ setup flag from Pine (toward unfilled HTF FVG, ±15m of hour)
    a_plus_target = _f(payload.get("a_plus_target"))   # near edge of that unfilled HTF FVG — the A+ TP
    # (Pine sends "0" when no usable level exists; place_order then falls back to the
    #  fixed A+ TP distance — 50pt NQ/MNQ, 15pt the S&P/Gold group.)

    if timeframe and ifvg_type:
        ifvg_info = f"{timeframe} {ifvg_type} IFVG"
    elif timeframe:
        inferred  = "Bullish" if action == "buy" else "Bearish"
        ifvg_info = f"{timeframe} {inferred} IFVG"
    else:
        ifvg_info = None

    session_stats["signals_received"] += 1
    sl_tag         = f" | SL ref: {sl_price}" if sl_price else " | SL: mode-based"
    imb_tag        = f" | imb {imbalance:.1f}t" if imbalance is not None else ""
    signal_display = f"{ifvg_info}  →  {action.upper()}" if ifvg_info else f"Signal: {action.upper()}"
    paused_tag     = " [BOT PAUSED]" if bot_paused else ""
    sym_tag        = f" | {sig_symbol}" if sig_symbol else ""
    await alog(f"SIGNAL  |  {signal_display}{sl_tag}{imb_tag}{sym_tag}{paused_tag}", "signal", ifvg_info,
               feed_msg=f"Signal · {signal_display}{sym_tag}{paused_tag}")

    # ── Route the signal to its instrument ────────────────────────────────────
    # The alert stamps its chart's instrument (symbol). A trade only fires if that
    # instrument is ticked-on. If an alert has no symbol AND exactly one instrument
    # is enabled, we assume it's for that one (backward compatible). With several
    # enabled and no symbol, we can't route safely, so it's ignored.
    target_symbol = sig_symbol
    if not target_symbol:
        if len(enabled_symbols) == 1:
            target_symbol = enabled_symbols[0]
        else:
            await alog("IGNORED — signal has no symbol and multiple instruments are enabled", "warning")
            return {"status": "ignored — no symbol"}

    if target_symbol not in INSTRUMENTS:
        await alog(f"IGNORED — {target_symbol} is not a supported instrument", "warning")
        return {"status": f"ignored — {target_symbol} unsupported"}

    if target_symbol not in enabled_symbols:
        await alog(f"IGNORED — {target_symbol} is not ticked on (enabled: {', '.join(enabled_symbols)})", "warning")
        return {"status": f"ignored — {target_symbol} not enabled"}

    # ── A+ gates — apply ONLY to A+ signals (configured in the A+ Setups card) ──
    # A+ otherwise bypasses every soft gate, so these are its own guard rails:
    #   • master off       → skip the A+ signal entirely
    #   • per-session cap  → at most N A+ entries per NY session (0 = unlimited)
    if action in ("buy", "sell") and a_plus:
        if not trade_settings.get("aplus_enabled", True):
            await alog(f"IGNORED — {target_symbol} A+ signal · A+ trading is OFF", "warning",
                       feed_msg=f"A+ off · {target_symbol}")
            return {"status": "ignored — A+ disabled"}
        _apmax = int(trade_settings.get("aplus_max_session", 0) or 0)
        if _apmax > 0:
            _sess    = _ny_session_id()
            _apcount = sum(1 for e in trade_log
                           if not e.get("separator") and e.get("a_plus") and e.get("session") == _sess)
            if _apcount >= _apmax:
                await alog(f"IGNORED — {target_symbol} A+ · session cap reached ({_apcount}/{_apmax})", "warning",
                           feed_msg=f"A+ cap {_apcount}/{_apmax} · {target_symbol}")
                return {"status": f"ignored — A+ session cap {_apmax}"}

    # ── 1-minute session gate — 5m always; 1m per ETH/NY mode ──────────────────
    # A+ setups bypass this (taken any time). 5m and other timeframes always pass.
    if action in ("buy", "sell") and not a_plus:
        _tfk = (timeframe or "").strip().lower()
        if _tfk in ("1", "1m", "1min") and not tf1m_allowed():
            await alog(
                f"IGNORED — {target_symbol} 1m signal · {tf1m_session} session only (now outside)",
                "warning", feed_msg=f"1m {tf1m_session}-only · {target_symbol}")
            return {"status": f"ignored — 1m {tf1m_session}-session only"}

    # ── Minimum FVG filter — disqualify tight-gap (low-quality) setups ─────────
    # The Pine indicator measures the inverted-FVG gap (only it sees the 3-candle
    # structure) and sends it as `imbalance` in ticks. Each instrument group has its
    # own floor (dashboard "Minimum FVG Ticks"): Nasdaq needs a wider gap than the
    # S&P/Gold group. A gap under the group floor is dropped here.
    if action in ["buy", "sell"] and imbalance is not None:
        _min_imb = min_fvg_ticks_for(target_symbol)
        if _min_imb > 0 and imbalance < _min_imb:
            await alog(
                f"IGNORED — {target_symbol} imbalance {imbalance:.1f}t < {_min_imb:.0f}t minimum "
                f"· {action.upper()} disqualified",
                "warning",
                feed_msg=f"Small FVG · {target_symbol} {imbalance:.1f}t<{_min_imb:.0f}t",
            )
            return {"status": f"ignored — imbalance {imbalance:.1f}t below {_min_imb:.0f}t"}

    # ── Direction bias — drop counter-trend signals on locked instruments ──────
    if action in ["buy", "sell"]:
        _side = 0 if action == "buy" else 1
        if not bias_allows(target_symbol, _side):
            await alog(
                f"IGNORED — {target_symbol} is {DIRECTION_LABELS[bias_for(target_symbol)]} · "
                f"{action.upper()} signal blocked",
                "warning",
                feed_msg=f"Blocked · {target_symbol} {DIRECTION_LABELS[bias_for(target_symbol)]}",
            )
            return {"status": f"ignored — {target_symbol} {bias_for(target_symbol)}-only"}

    # ── Max stop-distance cap — skip trades whose swing stop is too wide ───────
    # Applies to Swing mode only (the trade risks the full 15-bar swing distance).
    # A+ setups bypass this cap (they ride the tight structural candle-1 stop).
    if (action in ("buy", "sell") and not a_plus
            and trade_settings.get("swing_stop")
            and swing_sl is not None and entry_ref is not None):
        _cap = max_stop_ticks_for(target_symbol)
        if _cap > 0:
            _tk     = tick_for(target_symbol)
            _dist_t = abs(entry_ref - swing_sl) / _tk if _tk else 0
            if _dist_t > _cap:
                await alog(
                    f"IGNORED — {target_symbol} swing stop {_dist_t:.0f}t > {_cap:.0f}t cap "
                    f"(entry {entry_ref} → stop {swing_sl}) · trade skipped",
                    "warning",
                    feed_msg=f"Stop too wide · {target_symbol} {_dist_t:.0f}t>{_cap:.0f}t",
                )
                return {"status": f"ignored — stop {_dist_t:.0f}t over {_cap:.0f}t cap"}

    # (Per-instrument re-entry cooldown removed — the ONLY cooldown is the global
    # circuit breaker: 3 consecutive losses → 10-min pause → auto-resume.)

    if action in ["buy", "sell"]:
        asyncio.create_task(execute_trade(target_symbol, action, ifvg_info,
                                          sl_price=sl_price, entry_ref=entry_ref, timeframe=timeframe,
                                          c1_high=c1_high, c1_low=c1_low, swing_sl=swing_sl,
                                          a_plus=a_plus, a_plus_target=a_plus_target))
        return {"status": f"{target_symbol} {action.upper()} signal received"}

    await alog(f"IGNORED | Unknown action: '{action}'", "warning")
    return {"status": "ignored"}


@app.get("/api/state", dependencies=[Depends(require_dashboard_auth)])
async def get_state():
    # Per-instrument rows for the multi-instrument panel (every supported micro,
    # with an `enabled` flag so the UI can render toggles + live data together).
    instruments = []
    for sym, meta in INSTRUMENTS.items():
        c = contracts.get(sym) or {}
        instruments.append({
            "key":           sym,
            "label":         meta["label"],
            "tier":          _SYMBOL_TIER.get(sym, ""),
            "enabled":       sym in enabled_symbols,
            "contract":      c.get("id"),
            "contract_name": c.get("name"),
            "position":      positions.get(sym) or blank_pos(),
            "quote":         quotes.get(sym) or blank_quote(),
            "unrealized_pnl": unrealized_pnl(sym),
            "max":           max_contracts_for(sym),
            "qty":           qty_for(sym),
            "direction":     bias_for(sym),
        })
    _sz = sizing_summary()
    return {
        "bot_paused":               bot_paused,
        "circuit_breaker": {
            "active":     _breaker["active"],
            "threshold":  CIRCUIT_BREAKER_LOSSES,
            "resumes_in": max(0, int(_breaker["until"] - time.time())) if _breaker["active"] else 0,
            "trips":      _breaker.get("trips", 0),
        },
        "trading_mode":             trading_mode,
        "session_stats":            session_stats,
        "modes":                    MODES,
        "trade_settings":           trade_settings,
        "mode_presets":             MODE_PRESETS,
        "tp1_options":              TP1_OPTIONS,
        "tp2_options":              TP2_OPTIONS,
        "struct_tp_options":        list(STRUCT_TP_OPTIONS),
        "safety_net_pts":           SAFETY_NET_PTS,
        "sizing_summary":           _sz,
        "active_account":           active_account,
        "available_accounts":       available_accounts,
        "account_size":             account_size,
        "account_size_meta":        ACCOUNT_SIZE_META,
        "account_size_breakdown":   account_size_breakdown(),
        "min_fvg_groups":           MIN_FVG_GROUPS,
        "min_fvg_ticks":            min_fvg_ticks,
        "max_stop_ticks":           max_stop_ticks,
        "tf1m_session":             tf1m_session,
        "tf1m_session_options":     list(TF1M_SESSION_OPTIONS),
        "macro_window":             macro_window,
        "macro_window_options":     list(MACRO_WINDOW_OPTIONS),
        "presets":                  user_presets,
        "active_preset":            _active_preset_name(),
        "settings": {
            "enabled_symbols": enabled_symbols,
            "sizing_summary":  _sz,
            "start_time":      session_stats["start_time"],
        },
        "instruments":              instruments,
        "total_unrealized_pnl":     total_unrealized_pnl(),
        "recent_events":            list(event_log)[:60],
        "trade_log":                list(trade_log)[:100],
        # SignalR live data
        "signalr_user_connected":   signalr_user_connected,
        "signalr_market_connected": signalr_market_connected,
        "account_balance":          account_balance,
        "account_can_trade":        account_can_trade,
        # Adaptive sizing mode (opt-in) — settings + live ladder for the dashboard.
        "adaptive": {
            "enabled":       adaptive_enabled,
            "micros":        list(ADAPTIVE_MICROS),
            "settings":      adaptive_settings,
            "mult":          round(adaptive_state.get("mult", 1.0), 3),
            "next_micros":   adaptive.contracts_for(adaptive_state.get("mult", 1.0),
                                                    adaptive_settings.get("base_micros", 1)),
            "loss_run":      adaptive_state.get("loss_run", 0),
            "daily_losses":  adaptive_state.get("daily_losses", 0),
            "daily_limit":   adaptive_settings.get("daily_loss_limit", 10),
            "stopped":       adaptive_state.get("stopped", False),
        },
    }


@app.post("/api/pause", dependencies=[Depends(require_dashboard_auth)])
async def toggle_pause():
    global bot_paused
    bot_paused = not bot_paused
    state = "PAUSED" if bot_paused else "RESUMED"
    if not bot_paused:
        # A manual resume overrides and cancels any active circuit-breaker cooldown
        # (the scheduled auto-resume then no-ops because active is already False).
        _breaker["active"] = False
        _breaker["until"]  = 0.0
    await alog(f"Bot {state} — {'signals will be ignored' if bot_paused else 'trading active'}", "system")
    return {"bot_paused": bot_paused}


def _settings_desc() -> str:
    ts = trade_settings
    return (f"Stop {_stop_mode_label(ts)} · "
            f"TP {ts.get('structural_tp', '3x')}×stop · "
            f"Auto-BE {'ON' if ts['auto_be'] else 'OFF'} · "
            f"Macro {'ON' if ts.get('macro_time') else 'OFF'}")


@app.post("/api/set-trade-settings", dependencies=[Depends(require_dashboard_auth)])
async def set_trade_settings(request: Request):
    """Live-override individual trade settings (temporary until mode change)."""
    global trade_settings
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    ts = dict(trade_settings)
    if "tp1" in data:
        try:
            v = int(data["tp1"])
            if v in TP1_OPTIONS: ts["tp1"] = v
        except (ValueError, TypeError): pass
    if "tp2" in data:
        try:
            v = int(data["tp2"])
            if v in TP2_OPTIONS: ts["tp2"] = v
        except (ValueError, TypeError): pass
    for _k in STOP_MODE_KEYS:
        if _k in data:
            ts[_k] = bool(data[_k])
    if "auto_be" in data:
        ts["auto_be"] = bool(data["auto_be"])
    if "macro_time" in data:
        ts["macro_time"] = bool(data["macro_time"])
    if "structural_tp" in data and data["structural_tp"] in STRUCT_TP_OPTIONS:
        ts["structural_tp"] = data["structural_tp"]
    # A+ Setups card — master switch + fallback TP + session cap.
    if "aplus_enabled" in data:
        ts["aplus_enabled"] = bool(data["aplus_enabled"])
    for _ak in ("aplus_tp_nasdaq", "aplus_tp_spgold", "aplus_max_session"):
        if _ak in data:
            try:
                ts[_ak] = max(0, int(data[_ak]))
            except (ValueError, TypeError):
                pass
    _coerce_aplus(ts)
    # Stop modes are mutually exclusive — keep exactly one on. Honour whichever the
    # client just switched ON in this request (the dashboard sends both).
    _prefer = next((k for k in STOP_MODE_KEYS if k in data and ts.get(k)), None)
    _enforce_stop_mode(ts, prefer=_prefer)
    trade_settings = ts
    await alog(f"Settings → {_settings_desc()}", "system")
    save_state()
    return {"trade_settings": trade_settings}


@app.post("/api/toggle-instrument", dependencies=[Depends(require_dashboard_auth)])
async def toggle_instrument(request: Request):
    global enabled_symbols, _market_ws
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    sym = data.get("symbol")
    if sym not in INSTRUMENTS:
        return {"error": "Invalid symbol"}

    if sym in enabled_symbols:
        # Turning OFF — don't orphan an open position on that instrument.
        pos = positions.get(sym) or blank_pos()
        if pos["direction"] != "FLAT":
            await alog(f"{sym} can't be disabled — flatten its position first", "warning")
            return {"error": f"Flatten {sym} before disabling it"}
        if len(enabled_symbols) == 1:
            return {"error": "At least one instrument must stay enabled"}
        enabled_symbols = [s for s in enabled_symbols if s != sym]
        await alog(f"Instrument OFF → {sym}", "system")
    else:
        # Turning ON — resolve its contract right away.
        enabled_symbols = [s for s in INSTRUMENTS if s in enabled_symbols or s == sym]
        async with api_client() as client:
            token, _ = await get_auth(client, force=True)
            if token:
                await ensure_contracts(client, token, force=True)
        await alog(f"Instrument ON → {INSTRUMENTS[sym]['label']}", "system")

    save_state()
    # Drop the market socket so it reconnects subscribed to the new set.
    if _market_ws is not None:
        try:
            await _market_ws.close()
        except Exception:
            pass
    await push_state_update()
    return {"enabled_symbols": enabled_symbols}


@app.post("/api/toggle-group", dependencies=[Depends(require_dashboard_auth)])
async def toggle_group(request: Request):
    """Bulk-flip a whole contract tier (all Micro or all Mini contracts) in one
    click. If every contract in the tier is already on, this turns the tier OFF;
    otherwise it turns the whole tier ON. Honours the same guards as single toggles:
    never orphan an open position, and always keep at least one instrument enabled."""
    global enabled_symbols, _market_ws
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    tier  = (data.get("tier") or "").lower()
    group = CONTRACT_TIERS.get(tier)
    if not group:
        return {"error": "Invalid tier"}

    group_on = all(s in enabled_symbols for s in group)
    if group_on:
        # Turning the tier OFF — refuse if any member holds an open position, and
        # never empty the whole instrument set.
        blocked = [s for s in group
                   if (positions.get(s) or blank_pos())["direction"] != "FLAT"]
        if blocked:
            await alog(f"{tier.capitalize()} tier can't be disabled — flatten {', '.join(blocked)} first",
                       "warning")
            return {"error": f"Flatten {', '.join(blocked)} before disabling {tier} contracts"}
        remaining = [s for s in enabled_symbols if s not in group]
        if not remaining:
            return {"error": "At least one instrument must stay enabled"}
        enabled_symbols = remaining
        await alog(f"{tier.capitalize()} contracts OFF → {', '.join(group)}", "system")
    else:
        # Turning the tier ON — add every member, keep INSTRUMENTS panel order.
        enabled_symbols = [s for s in INSTRUMENTS if s in enabled_symbols or s in group]
        async with api_client() as client:
            token, _ = await get_auth(client, force=True)
            if token:
                await ensure_contracts(client, token, force=True)
        await alog(f"{tier.capitalize()} contracts ON → {', '.join(group)}", "system")

    save_state()
    # Drop the market socket so it reconnects subscribed to the new set.
    if _market_ws is not None:
        try:
            await _market_ws.close()
        except Exception:
            pass
    await push_state_update()
    return {"enabled_symbols": enabled_symbols}


@app.post("/api/set-direction", dependencies=[Depends(require_dashboard_auth)])
async def set_direction(request: Request):
    """Set a direction bias: both / long / short. 'long' blocks short signals,
    'short' blocks long signals. GROUP-LOCKED: because hedging is not allowed,
    setting the bias on any no-hedge-group member applies it to the WHOLE group
    (MNQ/NQ/MES/ES move together; MGC/GC move together). Does not touch positions."""
    global instrument_bias
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    sym  = data.get("symbol")
    bias = data.get("direction")
    if sym not in INSTRUMENTS:
        return {"error": "Invalid symbol"}
    if bias not in DIRECTION_OPTIONS:
        return {"error": "Invalid direction"}
    group = hedge_group(sym)          # applies to every member of the no-hedge group
    for s in group:
        if bias == "both":
            instrument_bias.pop(s, None)
        else:
            instrument_bias[s] = bias
    await alog(f"Direction · {'/'.join(sorted(group))} → {DIRECTION_LABELS[bias]} (group-locked)", "system",
               feed_msg=f"Bias · {'/'.join(sorted(group))} {DIRECTION_LABELS[bias]}")
    save_state()
    await push_state_update()
    return {"symbol": sym, "direction": bias, "group": sorted(group)}


@app.post("/api/set-account-size", dependencies=[Depends(require_dashboard_auth)])
async def set_account_size(request: Request):
    """Select the account-size risk preset (custom / 50k / 100k). Applies its
    per-instrument caps to ALL new positions immediately. Does not touch any open
    position — existing trades keep the size they were opened with."""
    global account_size
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    size = (data.get("account_size") or "").strip().lower()
    if size not in ACCOUNT_SIZE_META:
        return {"error": f"Invalid account size '{size}'"}
    account_size = size
    lbl = ACCOUNT_SIZE_META[size]["label"]
    await alog(f"Account size → {lbl} · sizing {sizing_summary()}", "system",
               feed_msg=f"Account size · {lbl}")
    save_state()
    await push_state_update()
    return {"account_size": account_size, "sizing_summary": sizing_summary()}


# ─── Adaptive sizing mode endpoints ───────────────────────────────────────────
@app.post("/api/adaptive/toggle", dependencies=[Depends(require_dashboard_auth)])
async def adaptive_toggle(request: Request):
    """Enable/disable adaptive sizing mode. When OFF the bot sizes exactly as before."""
    global adaptive_enabled
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    adaptive_enabled = bool(data.get("enabled"))
    await alog(f"Adaptive mode {'ENABLED' if adaptive_enabled else 'disabled'} · "
               f"micros {', '.join(ADAPTIVE_MICROS)} · mult {adaptive_state.get('mult', 1.0):.2f}",
               "system", feed_msg=f"Adaptive · {'on' if adaptive_enabled else 'off'}")
    save_state()
    await push_state_update()
    return {"adaptive_enabled": adaptive_enabled}


@app.post("/api/adaptive/settings", dependencies=[Depends(require_dashboard_auth)])
async def adaptive_set_settings(request: Request):
    """Update adaptive settings (ladder params + per-micro geometry). Validated and
    clamped by the engine; unknown/bad fields fall back to defaults."""
    global adaptive_settings
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    adaptive_settings = adaptive.sanitize_settings(data.get("settings") or data)
    await alog("Adaptive settings updated", "system", feed_msg="Adaptive · settings saved")
    save_state()
    await push_state_update()
    return {"adaptive_settings": adaptive_settings}


@app.post("/api/adaptive/resume", dependencies=[Depends(require_dashboard_auth)])
async def adaptive_resume(request: Request):
    """Clear the adaptive daily-loss stop (manual resume). Resets today's loss count;
    the risk multiplier is left untouched (it carries)."""
    adaptive_state["stopped"] = False
    adaptive_state["daily_losses"] = 0
    adaptive_state["day_key"] = _futures_day_key()
    await alog("Adaptive daily-loss stop cleared — trading resumed (multiplier unchanged)",
               "system", feed_msg="Adaptive · resumed")
    save_state()
    await push_state_update()
    return {"stopped": False}


@app.post("/api/adaptive/reset", dependencies=[Depends(require_dashboard_auth)])
async def adaptive_reset(request: Request):
    """Reset the ladder back to base (multiplier 1.0, streak 0). Does not change the
    daily-stop state or your settings."""
    adaptive_state["mult"] = 1.0
    adaptive_state["loss_run"] = 0
    await alog("Adaptive ladder reset to base (multiplier 1.00)", "system",
               feed_msg="Adaptive · ladder reset")
    save_state()
    await push_state_update()
    return {"mult": 1.0}


@app.post("/api/set-min-fvg", dependencies=[Depends(require_dashboard_auth)])
async def set_min_fvg(request: Request):
    """Set the minimum qualifying FVG (imbalance) size, in ticks, for one group.
    Applies immediately to new signals; existing positions are untouched."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    group = (data.get("group") or "").strip()
    if group not in MIN_FVG_GROUPS:
        return {"error": f"Invalid group '{group}'"}
    try:
        ticks = int(data.get("ticks"))
    except (TypeError, ValueError):
        return {"error": "ticks must be an integer"}
    if not (0 <= ticks <= 1000):
        return {"error": "ticks out of range (0–1000)"}
    min_fvg_ticks[group] = ticks
    lbl = MIN_FVG_GROUPS[group]["label"]
    await alog(f"Min FVG · {lbl} → {ticks} ticks", "system", feed_msg=f"Min FVG · {lbl} {ticks}t")
    save_state()
    await push_state_update()
    return {"min_fvg_ticks": min_fvg_ticks}


@app.post("/api/set-max-stop", dependencies=[Depends(require_dashboard_auth)])
async def set_max_stop(request: Request):
    """Set the max stop-distance cap (ticks) for one group. Swing entries whose
    stop is wider than this are skipped. 0 disables the cap for that group."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    group = (data.get("group") or "").strip()
    if group not in MIN_FVG_GROUPS:
        return {"error": f"Invalid group '{group}'"}
    try:
        ticks = int(data.get("ticks"))
    except (TypeError, ValueError):
        return {"error": "ticks must be an integer"}
    if not (0 <= ticks <= 10000):
        return {"error": "ticks out of range (0–10000)"}
    max_stop_ticks[group] = ticks
    lbl = MIN_FVG_GROUPS[group]["label"]
    await alog(f"Max stop · {lbl} → {ticks} ticks", "system", feed_msg=f"Max stop · {lbl} {ticks}t")
    save_state()
    await push_state_update()
    return {"max_stop_ticks": max_stop_ticks}


@app.post("/api/set-tf1m-session", dependencies=[Depends(require_dashboard_auth)])
async def set_tf1m_session(request: Request):
    """Set the 1-minute session mode: ETH (all session) or NY (09:30–16:00 NY)."""
    global tf1m_session
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    mode = str(data.get("session") or "").upper()
    if mode not in TF1M_SESSION_OPTIONS:
        return {"error": f"session must be one of {', '.join(TF1M_SESSION_OPTIONS)}"}
    tf1m_session = mode
    await alog(f"1m session → {mode}" + (" (09:30–16:00 NY)" if mode == "NY" else " (all session)"),
               "system", feed_msg=f"1m · {mode}")
    save_state()
    await push_state_update()
    return {"tf1m_session": tf1m_session}


@app.post("/api/set-macro-window", dependencies=[Depends(require_dashboard_auth)])
async def set_macro_window(request: Request):
    """Set the Macro window width (minutes): first N + last N min of each NY hour
    count as macro. One of MACRO_WINDOW_OPTIONS (5 / 10 / 15)."""
    global macro_window
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        v = int(data.get("minutes"))
    except (ValueError, TypeError):
        return {"error": "minutes required"}
    if v not in MACRO_WINDOW_OPTIONS:
        return {"error": f"minutes must be one of {', '.join(map(str, MACRO_WINDOW_OPTIONS))}"}
    macro_window = v
    await alog(f"Macro window → first/last {v} min of each hour", "system",
               feed_msg=f"Macro ±{v}m")
    save_state()
    await push_state_update()
    return {"macro_window": macro_window}


@app.post("/api/test-notify", dependencies=[Depends(require_dashboard_auth)])
async def test_notify():
    """Send a one-off test alert to every configured push channel (Discord +
    Telegram), so you can confirm wiring without waiting for a live A+ setup."""
    channels = []
    if DISCORD_WEBHOOK_URL:
        channels.append("Discord")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        channels.append("Telegram")
    if not channels:
        return {"error": "No alert channel configured — set DISCORD_WEBHOOK_URL (and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) in .env and restart"}
    await notify_push("✅ KAIROS alerts are live — you'll get a ping here whenever an A+ setup is taken.")
    await alog(f"[TEST] Push alert sent → {', '.join(channels)}", "system", feed_msg=f"Test alert → {', '.join(channels)}")
    return {"status": f"test alert sent to {', '.join(channels)}", "channels": channels}


@app.post("/api/save-preset", dependencies=[Depends(require_dashboard_auth)])
async def save_preset(request: Request):
    """Snapshot the current settings under a name. Overwrites a same-named preset.
    The new preset becomes the active selection."""
    global user_presets, active_preset
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = (data.get("name") or "").strip()
    if not name:
        return {"error": "Preset name required"}
    if len(name) > PRESET_NAME_MAX:
        return {"error": f"Name too long (max {PRESET_NAME_MAX} chars)"}
    if name not in user_presets and len(user_presets) >= PRESET_LIMIT:
        return {"error": f"Preset limit reached ({PRESET_LIMIT}) — delete one first"}
    existed = name in user_presets
    user_presets[name] = _settings_snapshot()
    active_preset = name
    await alog(f"Preset {'updated' if existed else 'saved'} · {name} | {_preset_desc(user_presets[name])}",
               "system", feed_msg=f"Preset {'updated' if existed else 'saved'} · {name}")
    save_state()
    await push_state_update()
    return {"ok": True, "presets": user_presets, "active_preset": active_preset}


@app.post("/api/apply-preset", dependencies=[Depends(require_dashboard_auth)])
async def apply_preset(request: Request):
    """Apply a saved preset → loads all its settings live. Open positions, the
    broker account and pause state are NOT touched."""
    global active_preset
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = (data.get("name") or "").strip()
    ps = user_presets.get(name)
    if not isinstance(ps, dict):
        return {"error": f"Unknown preset '{name}'"}
    _apply_settings_snapshot(ps)
    active_preset = name
    await alog(f"Preset applied · {name} | {_preset_desc(ps)}", "system",
               feed_msg=f"Preset · {name}")
    save_state()
    await push_state_update()
    return {"ok": True, "active_preset": active_preset}


@app.post("/api/delete-preset", dependencies=[Depends(require_dashboard_auth)])
async def delete_preset(request: Request):
    """Delete a saved preset. If it was the active one, the selection clears to
    default (live settings are left as they are)."""
    global user_presets, active_preset
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = (data.get("name") or "").strip()
    if name not in user_presets:
        return {"error": f"Unknown preset '{name}'"}
    del user_presets[name]
    if active_preset == name:
        active_preset = None
    await alog(f"Preset deleted · {name}", "system", feed_msg=f"Preset deleted · {name}")
    save_state()
    await push_state_update()
    return {"ok": True, "presets": user_presets, "active_preset": active_preset}


@app.post("/api/set-account", dependencies=[Depends(require_dashboard_auth)])
async def set_account(request: Request):
    """Switch the active trading account. Refused while any position is open on the
    current account (switching would orphan it). Forces the user socket to
    re-subscribe to the new account's positions/orders/fills/balance."""
    global active_account, _user_ws
    try:
        data = await request.json()
        name = (data.get("account") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not name:
        return {"error": "No account given"}
    if available_accounts and name not in available_accounts:
        return {"error": f"Unknown account '{name}'"}
    if name == active_account:
        return {"active_account": active_account, "available_accounts": available_accounts}
    # Safety: never switch while holding a position on the current account.
    open_syms = [s for s in enabled_symbols
                 if (positions.get(s) or blank_pos())["direction"] != "FLAT"]
    if open_syms:
        await alog(f"Account switch BLOCKED — open position on {', '.join(open_syms)}; flatten first", "warning")
        return {"error": f"Flatten {', '.join(open_syms)} first"}
    prev = active_account
    active_account = name
    # Invalidate the cached auth so the next call re-resolves the new account_id,
    # and clear stale per-account position/BE/structural state.
    _auth_cache.update(token=None, account_id=None, ts=0.0)
    for s in list(positions.keys()):
        positions[s] = blank_pos()
    be_state.clear()
    structural_pos.clear()
    current_bracket_ids.clear()
    save_state()
    await alog(f"Account → {name} (was {prev})", "system")
    # Drop the user socket so it reconnects subscribed to the new account.
    if _user_ws is not None:
        try:
            await _user_ws.close()
        except Exception:
            pass
    asyncio.create_task(_reconcile_account_now())
    await push_state_update()
    return {"active_account": active_account, "available_accounts": available_accounts}


async def _reconcile_account_now():
    """After an account switch, give the user socket a moment to reconnect, then
    pull the new account's open positions and session P&L."""
    await asyncio.sleep(1.5)
    try:
        async with api_client() as client:
            token, account_id = await get_auth(client, force=True)
            if token and account_id:
                await refresh_positions(client, token, account_id)
                await reconcile_session_pnl(client, token, account_id)
    except Exception:
        pass
    await push_state_update()


@app.post("/api/flatten", dependencies=[Depends(require_dashboard_auth)])
async def flatten_all():
    await alog("FLATTEN ALL requested from dashboard", "system")
    # Share the trade lock so a manual flatten can't interleave with a signal
    # that's mid-execution (which could otherwise re-open right as we close).
    async with trade_lock:
      async with api_client() as client:
        token, account_id = await get_auth(client)
        if not token or not account_id:
            return {"error": "Auth failed"}
        await ensure_contracts(client, token)
        # Cancel + flatten EVERY enabled instrument that has a position.
        closed = []
        for sym in list(enabled_symbols):
            await cancel_all_orders(client, token, account_id, sym)
            size, side = await refresh_positions(client, token, account_id, only_symbol=sym)
            if size and size > 0:
                ok = await flatten_position(client, token, account_id, sym, size, side)
                closed.append(f"{sym}{'' if ok else '(failed)'}")
        if not closed:
            await alog("FLATTEN ALL — already flat, all orders cancelled", "system")
            return {"status": "flat"}
        return {"status": "flattened", "instruments": closed}


@app.post("/api/test-signal", dependencies=[Depends(require_dashboard_auth)])
async def test_signal(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    action = data.get("action", "buy").lower()
    sym    = data.get("symbol") or (enabled_symbols[0] if enabled_symbols else "MNQ")
    if sym not in enabled_symbols:
        return {"error": f"{sym} is not enabled"}
    await alog(f"[TEST] Firing test {action.upper()} signal on {sym}", "signal")
    asyncio.create_task(execute_trade(sym, action, ifvg_info="[TEST]", is_test=True))
    return {"status": f"test {action} fired on {sym}"}


@app.get("/api/events", dependencies=[Depends(require_dashboard_auth)])
async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    sse_clients.append(queue)

    async def generator():
        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            try: sse_clients.remove(queue)
            except ValueError: pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request,
                    token: str = Query(None),
                    dash: str = Cookie(None)):
    # Human-facing control panel. Unlike the /api/* endpoints (which stay on the
    # hard-401 require_dashboard_auth dependency), a person who lands here without
    # a valid token gets a friendly login page — not a raw 401 JSON blob — so the
    # navbar's "Dashboard" link is safe to expose publicly.
    if not DASHBOARD_TOKEN:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN not configured")

    provided = token or dash
    if not provided or not hmac.compare_digest(provided, DASHBOARD_TOKEN):
        # Absent/wrong token → serve the login page (401). Flag a *failed* attempt
        # only when a credential was actually supplied, so a clean first visit
        # shows the bare form without an "incorrect token" banner. The token is
        # never logged or echoed back into the page.
        denied = "1" if provided else ""
        return HTMLResponse(content=_read_page(_LOGIN_PATH).replace("__DENIED__", denied),
                            status_code=401)

    # Valid → serve the dashboard. Inject the token so the page's fetch calls can
    # authenticate via the X-Dashboard-Token header. token is JSON-encoded to
    # safely embed it inside the JS string literal.
    tok  = token or dash or ""
    html = _read_page(_DASHBOARD_PATH).replace("__DASHBOARD_TOKEN__", json.dumps(tok)[1:-1])
    resp = HTMLResponse(content=html)
    if token:
        # First visit arrived with ?token=… — move the credential into an HttpOnly
        # cookie so SSE and future page loads never carry it in the URL (browser
        # history / ngrok inspector). Secure flag only when actually on HTTPS,
        # otherwise the local http://127.0.0.1:8000 dashboard would drop it.
        secure = (request.url.scheme == "https"
                  or request.headers.get("x-forwarded-proto") == "https")
        resp.set_cookie("dash", token, httponly=True, samesite="lax",
                        secure=secure, max_age=30 * 24 * 3600, path="/")
    return resp


# ─── Public / control pages (served from web/*.html) ───────────────────────
# Each page is its own file so the markup/CSS/JS can be edited as real front-end
# code (no Python triple-quoted-string escaping gotchas). They are re-read PER
# REQUEST — the files are small and the traffic is light — so edits to
# site/index.html · web/login.html · web/dashboard.html appear on a browser
# refresh with NO bot restart. Only the served HTML is affected; trading logic
# is untouched.
# __DENIED__ (login) and __DASHBOARD_TOKEN__ (dashboard) are substituted per
# request by their routes above. (The landing page is site/index.html — see below.)
_WEB_DIR        = _pathlib.Path(__file__).parent / "web"
# The PUBLIC landing page now lives in ../site/ — it is hosted independently on
# Cloudflare Pages at https://your-domain.example so it stays up even when this bot
# is off (the bot only owns the control/webhook origin, https://app.your-domain.example).
# The bot still serves the SAME file at its own "/" as a harmless fallback, so
# site/index.html is the single source of truth for the landing markup.
_LANDING_PATH   = _pathlib.Path(__file__).parent / "site" / "index.html"
_LOGIN_PATH     = _WEB_DIR / "login.html"
_DASHBOARD_PATH = _WEB_DIR / "dashboard.html"


def _read_page(path: _pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def landing():
    # Public, unauthenticated face of your-domain.example. This is a static
    # marketing/"show off the bot" page only — it deliberately discloses NO live
    # trading posture (pause state, mode, instruments, contract IDs, P&L). The
    # control dashboard stays gated at /dashboard; machine health is at /health.
    return HTMLResponse(content=_read_page(_LANDING_PATH))


@app.get("/health")
@app.get("/healthz")
async def health():
    # Machine-readable liveness probe (uptime monitors / tunnel checks). Kept
    # deliberately minimal so it never leaks trading posture.
    return {"status": "ok"}


@app.get("/robots.txt")
async def robots():
    # The public landing (/) is indexable; the control panel, login, and API are
    # not. login.html + dashboard.html also carry a noindex meta as a backstop.
    body = ("User-agent: *\n"
            "Disallow: /dashboard\n"
            "Disallow: /api/\n")
    return Response(content=body, media_type="text/plain")


# ─── Dashboard control panel ───────────────────────────────────────────────
# The dashboard markup/CSS/JS now lives in web/dashboard.html (extracted from a
# ~900-line inline string). It is loaded per request by the /dashboard route via
# _read_page(_DASHBOARD_PATH); __DASHBOARD_TOKEN__ is substituted there. Edit
# web/dashboard.html and refresh the browser — no bot restart needed.




if __name__ == "__main__":
    import uvicorn
    # Bind to localhost only — the Cloudflare tunnel forwards to 127.0.0.1:8000,
    # so there's no reason to expose the raw port on every interface (0.0.0.0).
    # reload=False on purpose: the auto-reloader is a footgun for a live trading
    # bot — a mid-session edit can wedge the worker (port held, nothing served,
    # webhooks silently dropped during market hours). Restart manually instead.
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
