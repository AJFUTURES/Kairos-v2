"""Read-only trade-history normalization and separate human review storage.

This module deliberately has no broker, FastAPI, or bot-state side effects.  The
Trade Centre can therefore parse and test historical records without starting
KAIROS or touching live trading behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "1.0"
CALCULATION_VERSION = "1.0"
NY_TZ = ZoneInfo("America/New_York")

# Dollar value of one full point for the normalized futures symbols KAIROS uses.
POINT_VALUE_USD = {
    "MNQ": 2.0,
    "NQ": 20.0,
    "MES": 5.0,
    "ES": 50.0,
    "MGC": 10.0,
    "GC": 100.0,
    "CL": 1000.0,
}

SESSION_RE = re.compile(r"^SESSION\s+(\d{4}-\d{2}-\d{2})\s+\((.*?)\)")
TRADE_RE = re.compile(
    r"^\[entry\s+(?P<entry>.*?)\s+NY\s+->\s+exit\s+(?P<exit>.*?)\s+NY\]\s+"
    r"(?P<inst>[A-Z]{1,4})\s+(?P<dir>BUY|SELL)\s+x(?P<size>\d+)\s+"
    r"(?P<strategy>.*?)\s+->\s+(?P<outcome>.*?)\s+"
    r"(?P<sign>[-+])\$(?P<pnl>[\d,]+(?:\.\d+)?)"
    r"(?:\s+\((?P<details>.*?)\))?\s*$"
)
PLAN_RE = re.compile(
    r"SL\s+(?P<sl>[\d.]+)pt\s*/\s*TP\s+(?P<tp>[\d.]+)pt"
    r"(?:\s+@\s+exit\s+(?P<exitpx>[\d.]+))?"
    r"(?:\s*·\s*(?P<source>.*?))?(?:\s+\[A\+\])?$"
)
SOURCE_RE = re.compile(r"(?P<tf>\d+\s*[mMhH])\s+(?P<bias>Bullish|Bearish)\s+IFVG", re.I)
TAG_RE = re.compile(r"[^A-Za-z0-9 +_-]+")
_review_lock = threading.Lock()


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return default if text in ("", "—") else text


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value)
    if not text:
        return None
    try:
        return float(text.replace("$", "").replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _integer(value: Any, default: int = 1) -> int:
    try:
        parsed = int(float(str(value)))
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _session_datetime(session_id: str, clock_text: str) -> datetime | None:
    """Convert a legacy NY display time plus 6 PM session ID to an aware datetime.

    Session IDs name the date of the 6 PM New York open. Times before 6 PM occur
    on the following calendar day. Legacy rows have minute precision only, so the
    result is intentionally labelled approximate by callers.
    """
    if not session_id or not clock_text:
        return None
    parsed_time = None
    cleaned = clock_text.replace("NY", "").strip()
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            parsed_time = datetime.strptime(cleaned, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None
    try:
        session_date = datetime.strptime(session_id, "%Y-%m-%d").date()
    except ValueError:
        return None
    trade_date = session_date if parsed_time >= time(18, 0) else session_date + timedelta(days=1)
    return datetime.combine(trade_date, parsed_time, tzinfo=NY_TZ)


def _iso_utc(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _session_name(entry_ny: datetime | None) -> str:
    if entry_ny is None:
        return "Unknown"
    minutes = entry_ny.hour * 60 + entry_ny.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "New York"
    if 3 * 60 <= minutes < 9 * 60 + 30:
        return "London"
    return "Asia"


def _source_parts(source: str) -> tuple[str | None, str | None]:
    match = SOURCE_RE.search(source or "")
    if not match:
        return None, None
    return match.group("tf").replace(" ", "").lower(), match.group("bias").title()


def _outcome_group(outcome: str, pnl: float | None) -> str:
    lower = (outcome or "").lower()
    if "break-even" in lower or "be hit" in lower:
        return "Break-even"
    if "stop" in lower:
        return "Stop"
    if "tp" in lower:
        return "TP hit"
    if "close-based" in lower:
        return "Close-based"
    if "manual" in lower or "reversal" in lower:
        return "Manual / reversal"
    if pnl is None:
        return "Unknown"
    if pnl > 0:
        return "Win"
    if pnl < 0:
        return "Loss"
    return "Break-even"


def _base_key(record: dict[str, Any]) -> str:
    parts = (
        record.get("session_id"), record.get("entry_time_ny"), record.get("instrument"),
        record.get("direction"), record.get("quantity"),
    )
    return "|".join(str(part or "") for part in parts)


def _trade_id(record: dict[str, Any], occurrence: int = 1) -> str:
    explicit = _clean_text(record.get("trade_id"))
    if explicit:
        return explicit
    seed = f"{_base_key(record)}|{occurrence}"
    return "ktr_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _finalize_record(record: dict[str, Any], occurrence: int = 1) -> dict[str, Any]:
    session_id = _clean_text(record.get("session_id"))
    entry_time = _clean_text(record.get("entry_time_ny"))
    exit_time = _clean_text(record.get("exit_time_ny"))
    entry_ny = _session_datetime(session_id, entry_time)
    exit_ny = _session_datetime(session_id, exit_time)
    if entry_ny and exit_ny and exit_ny < entry_ny:
        exit_ny += timedelta(days=1)

    instrument = _clean_text(record.get("instrument"), "Unknown").upper()
    quantity = _integer(record.get("quantity"))
    sl_points = _number(record.get("stop_points"))
    tp_points = _number(record.get("target_points"))
    pnl = _number(record.get("net_pnl_usd"))
    point_value = POINT_VALUE_USD.get(instrument)
    planned_risk = sl_points * point_value * quantity if sl_points and point_value else None
    planned_reward = tp_points * point_value * quantity if tp_points and point_value else None
    planned_rr = tp_points / sl_points if sl_points and tp_points and sl_points > 0 else None
    realized_r = pnl / planned_risk if pnl is not None and planned_risk else None
    source = _clean_text(record.get("ifvg_source"))
    timeframe, ifvg_direction = _source_parts(source)

    warnings = list(dict.fromkeys(record.get("data_quality_warnings") or []))
    if not entry_ny:
        warnings.append("Entry timestamp is unavailable.")
    else:
        warnings.append("Legacy timestamp reconstructed from session date and display time.")
    if record.get("exit_price_is_proxy"):
        warnings.append("Exit price is a live-quote proxy, not a confirmed broker fill.")
    if _number(record.get("entry_fill_price")) is None:
        warnings.append("Exact entry fill price is unavailable in this legacy record.")
    if not _clean_text(record.get("account_alias")):
        warnings.append("The originating account was not stored on this trade.")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "trade_id": _trade_id(record, occurrence),
        "session_id": session_id or None,
        "session_name": _session_name(entry_ny),
        "entry_at_utc": _iso_utc(entry_ny),
        "exit_at_utc": _iso_utc(exit_ny),
        "entry_time_ny": entry_time or None,
        "exit_time_ny": exit_time or None,
        "weekday": entry_ny.strftime("%a") if entry_ny else None,
        "entry_hour_ny": entry_ny.strftime("%H:%M") if entry_ny else None,
        "instrument": instrument,
        "contract_id": _clean_text(record.get("contract_id")) or None,
        "account_alias": _clean_text(record.get("account_alias")) or None,
        "direction": _clean_text(record.get("direction"), "Unknown").upper(),
        "quantity": quantity,
        "setup_type": "A+ IFVG" if bool(record.get("a_plus")) else "IFVG",
        "a_plus": bool(record.get("a_plus")),
        "timeframe": timeframe,
        "ifvg_direction": ifvg_direction,
        "ifvg_source": source or None,
        "stop_mode": _clean_text(record.get("stop_mode"), "Unknown"),
        "entry_fill_price": _number(record.get("entry_fill_price")),
        "exit_fill_price": _number(record.get("exit_fill_price")),
        "exit_price_proxy": _number(record.get("exit_price_proxy")),
        "stop_points": sl_points,
        "target_points": tp_points,
        "planned_risk_usd": round(planned_risk, 2) if planned_risk is not None else None,
        "planned_reward_usd": round(planned_reward, 2) if planned_reward is not None else None,
        "planned_rr": round(planned_rr, 3) if planned_rr is not None else None,
        "be_fired": bool(record.get("be_fired")),
        "reached_phase2": bool(record.get("reached_phase2")),
        "exit_reason": _clean_text(record.get("exit_reason")) or None,
        "outcome": _clean_text(record.get("outcome"), "Unknown"),
        "outcome_group": _outcome_group(_clean_text(record.get("outcome")), pnl),
        "gross_pnl_usd": _number(record.get("gross_pnl_usd")),
        "fees_usd": _number(record.get("fees_usd")),
        "net_pnl_usd": round(pnl, 2) if pnl is not None else None,
        "realized_r": round(realized_r, 3) if realized_r is not None else None,
        "data_sources": sorted(set(record.get("data_sources") or [])),
        "data_quality_warnings": list(dict.fromkeys(warnings)),
    }
    return normalized


def parse_results_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse finalized rows from the existing human-readable results.txt format."""
    raw_records: list[dict[str, Any]] = []
    parse_warnings: list[str] = []
    session_id = ""
    for line_number, raw_line in enumerate((text or "").splitlines(), 1):
        line = raw_line.strip()
        session_match = SESSION_RE.match(line)
        if session_match:
            session_id = session_match.group(1)
            continue
        if not line.startswith("[entry "):
            continue
        match = TRADE_RE.match(line)
        if not match:
            parse_warnings.append(f"Could not parse results.txt line {line_number}.")
            continue
        details = match.group("details") or ""
        plan_match = PLAN_RE.search(details)
        stop_points = target_points = exit_proxy = None
        source = ""
        if plan_match:
            stop_points = _number(plan_match.group("sl"))
            target_points = _number(plan_match.group("tp"))
            exit_proxy = _number(plan_match.group("exitpx"))
            source = _clean_text(plan_match.group("source"))
            source = source.replace(" [A+]", "").strip()
        elif details:
            parse_warnings.append(f"Trade plan details were incomplete on results.txt line {line_number}.")

        pnl = _number(match.group("pnl")) or 0.0
        if match.group("sign") == "-":
            pnl = -pnl
        raw_records.append({
            "session_id": session_id,
            "entry_time_ny": match.group("entry"),
            "exit_time_ny": match.group("exit"),
            "instrument": match.group("inst"),
            "direction": match.group("dir"),
            "quantity": match.group("size"),
            "stop_mode": match.group("strategy").strip(),
            "a_plus": "[A+]" in details or match.group("strategy").strip().startswith("A+"),
            "outcome": match.group("outcome").strip(),
            "net_pnl_usd": pnl,
            "stop_points": stop_points,
            "target_points": target_points,
            "exit_price_proxy": exit_proxy,
            "exit_price_is_proxy": exit_proxy is not None,
            "ifvg_source": source,
            "data_sources": ["results.txt"],
        })
    return raw_records, parse_warnings


def normalize_working_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert finalized in-memory/bot_state trade rows to the shared raw shape."""
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("separator"):
            continue
        pnl = _number(row.get("pnl"))
        if pnl is None:
            continue
        normalized.append({
            "trade_id": row.get("trade_id"),
            "session_id": row.get("session"),
            "entry_time_ny": row.get("time_ny"),
            "exit_time_ny": row.get("exit_ny"),
            "instrument": row.get("instrument"),
            "contract_id": row.get("contract_id"),
            "account_alias": row.get("account_alias"),
            "direction": row.get("direction"),
            "quantity": row.get("size"),
            "stop_mode": row.get("stop_mode"),
            "a_plus": row.get("a_plus"),
            "outcome": row.get("outcome"),
            "net_pnl_usd": pnl,
            "stop_points": row.get("sl_pts"),
            "target_points": row.get("tp_pts"),
            "entry_fill_price": row.get("entry_price"),
            "exit_fill_price": row.get("exit_fill_price"),
            "exit_price_proxy": row.get("exit_price"),
            "exit_price_is_proxy": row.get("exit_price") is not None and row.get("exit_fill_price") is None,
            "ifvg_source": row.get("ifvg_source"),
            "be_fired": row.get("be_fired"),
            "reached_phase2": row.get("reached_phase2"),
            "exit_reason": row.get("exit_reason"),
            "gross_pnl_usd": row.get("gross_pnl_usd"),
            "fees_usd": row.get("fees_usd"),
            "data_sources": ["bot_state trade_log"],
        })
    return normalized


def merge_history(results_records: list[dict[str, Any]], working_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the durable text summary with richer recent working rows.

    The legacy records have no true ID, so the shared session/time/instrument/
    direction/quantity tuple is the safest available compatibility key. Duplicate
    keys are matched in encounter order and receive deterministic occurrence IDs.
    """
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in results_records:
        buckets[_base_key(record)].append(dict(record))

    unmatched_working = []
    consumed = Counter()
    for working in working_records:
        key = _base_key(working)
        index = consumed[key]
        if index < len(buckets.get(key, [])):
            base = buckets[key][index]
            merged = {**base, **{k: v for k, v in working.items() if v not in (None, "", "—")}}
            merged["data_sources"] = sorted(set(base.get("data_sources", []) + working.get("data_sources", [])))
            buckets[key][index] = merged
            consumed[key] += 1
        else:
            unmatched_working.append(dict(working))

    raw_all = [record for records in buckets.values() for record in records] + unmatched_working
    occurrence = Counter()
    finalized = []
    for record in raw_all:
        key = _base_key(record)
        occurrence[key] += 1
        finalized.append(_finalize_record(record, occurrence[key]))
    finalized.sort(key=lambda row: (row.get("entry_at_utc") or "", row.get("trade_id") or ""), reverse=True)
    return finalized


def load_reviews(path: str | Path) -> dict[str, dict[str, Any]]:
    review_path = Path(path)
    if not review_path.exists():
        return {}
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    reviews = payload.get("reviews", {}) if isinstance(payload, dict) else {}
    return reviews if isinstance(reviews, dict) else {}


def save_review(path: str | Path, trade_id: str, notes: Any, tags: Any) -> dict[str, Any]:
    clean_id = _clean_text(trade_id)
    if not clean_id or len(clean_id) > 96 or not re.fullmatch(r"[A-Za-z0-9_-]+", clean_id):
        raise ValueError("Invalid trade ID")
    clean_notes = _clean_text(notes)[:4000]
    clean_tags = []
    for raw_tag in tags if isinstance(tags, list) else []:
        tag = TAG_RE.sub("", _clean_text(raw_tag))[:32].strip()
        if tag and tag.lower() not in {existing.lower() for existing in clean_tags}:
            clean_tags.append(tag)
        if len(clean_tags) == 10:
            break
    review = {
        "notes": clean_notes,
        "tags": clean_tags,
        "reviewed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with _review_lock:
        reviews = load_reviews(review_path)
        reviews[clean_id] = review
        payload = {"schema_version": 1, "reviews": reviews}
        temp_path = review_path.with_suffix(review_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_path, review_path)
    return review


def build_trade_centre_payload(
    results_path: str | Path,
    working_rows: Iterable[dict[str, Any]],
    reviews_path: str | Path,
) -> dict[str, Any]:
    results_file = Path(results_path)
    parse_warnings: list[str] = []
    result_records: list[dict[str, Any]] = []
    if results_file.exists():
        try:
            result_records, parse_warnings = parse_results_text(results_file.read_text(encoding="utf-8"))
        except OSError as exc:
            parse_warnings.append(f"Could not read results.txt: {exc.__class__.__name__}.")
    working_records = normalize_working_rows(working_rows)
    trades = merge_history(result_records, working_records)
    reviews = load_reviews(reviews_path)
    for trade in trades:
        trade["review"] = reviews.get(trade["trade_id"], {"notes": "", "tags": [], "reviewed_at_utc": None})

    source_counts = Counter(source for trade in trades for source in trade.get("data_sources", []))
    warning_count = sum(1 for trade in trades if trade.get("data_quality_warnings"))
    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "calculation_version": CALCULATION_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "default_timezone": "America/New_York",
            "currency": "USD",
            "trade_count": len(trades),
            "records_with_warnings": warning_count,
            "source_counts": dict(source_counts),
            "parse_warnings": parse_warnings,
            "limitations": [
                "Legacy history does not store the originating account on each trade.",
                "Legacy timestamps are reconstructed from session date and display time.",
                "Some legacy exit prices are quote proxies rather than confirmed fills.",
            ],
        },
        "definitions": {
            "win_rate": "Positive net-P&L trades divided by all finalized trades; break-even remains in the denominator.",
            "profit_factor": "Gross winning P&L divided by absolute gross losing P&L.",
            "expectancy": "Net P&L divided by finalized trade count.",
            "max_drawdown": "Largest peak-to-trough decline in cumulative net P&L, beginning at zero.",
            "realized_r": "Net P&L divided by initial planned dollar risk; derived only when stop distance and point value are known.",
        },
        "trades": trades,
    }
