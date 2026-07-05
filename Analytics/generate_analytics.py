#!/usr/bin/env python3
"""
KAIROS trade-analytics dashboard generator.

Reads results.txt (one line per CLOSED trade, grouped by session) and writes
kairos_analytics.html — a self-contained dashboard with equity curve, P&L over
time, per-instrument and per-strategy breakdowns, win-rate / expectancy stats,
and a sortable/filterable trade table.

Run:  python3 generate_analytics.py
Re-run any time results.txt changes (the daily scheduled task does this).
"""

import json, re, sys, os, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# results.txt is written by the bot in the KAIROS root (this folder's parent).
SRC = os.path.join(os.path.dirname(HERE), "results.txt")
OUT = os.path.join(HERE, "kairos_analytics.html")
TPL = os.path.join(HERE, "analytics_template.html")

SESSION_RE = re.compile(r"^SESSION\s+(\d{4}-\d{2}-\d{2})\s+\((.*?)\)")
TRADE_RE = re.compile(
    r"^\[entry\s+(?P<entry>.*?)\s+->\s+exit\s+(?P<exit>.*?)\]\s+"
    r"(?P<inst>[A-Z]{2,3})\s+(?P<dir>BUY|SELL)\s+x(?P<size>\d+)\s+"
    r"(?P<strat>.*?)\s+->\s+(?P<outcome>.*?)\s+(?P<sign>[-+])\$(?P<pnl>[\d,]+\.\d+)"
    r"(?:\s+\((?P<paren>.*?)\))?\s*$"
)
PAREN_RE = re.compile(
    r"SL\s+(?P<sl>[\d.]+)pt\s*/\s*TP\s+(?P<tp>[\d.]+)pt"
    r"(?:\s+@\s+exit\s+(?P<exitpx>[\d.]+))?"
    r"\s*·\s*(?P<source>.*?)\s*$"
)

def classify_outcome(o):
    o = o.lower()
    if o.startswith("tp"): return "TP hit"
    if "break-even" in o or "be hit" in o: return "Break-even"
    if o.startswith("stop"): return "Stop"
    if "close-based" in o: return "Close-based"
    return o

def parse():
    trades, session, session_label = [], None, None
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            ms = SESSION_RE.match(line)
            if ms:
                session, session_label = ms.group(1), ms.group(2)
                continue
            mt = TRADE_RE.match(line)
            if not mt:
                continue
            pnl = float(mt.group("pnl").replace(",", ""))
            if mt.group("sign") == "-":
                pnl = -pnl
            sl = tp = None; source = ""; aplus = False; tf = ""; ifvg = ""
            paren = mt.group("paren") or ""
            mp = PAREN_RE.search(paren)
            if mp:
                sl = float(mp.group("sl")); tp = float(mp.group("tp"))
                source = mp.group("source").strip()
            if "[A+]" in paren or mt.group("strat").startswith("A+"):
                aplus = True
            msrc = re.search(r"(\dm)\s+(Bullish|Bearish)\s+IFVG", source or paren)
            if msrc:
                tf = msrc.group(1); ifvg = msrc.group(2)
            rr = round(tp / sl, 2) if (sl and tp and sl > 0) else None
            trades.append({
                "session": session, "session_label": session_label,
                "entry": mt.group("entry"), "exit": mt.group("exit"),
                "inst": mt.group("inst"), "dir": mt.group("dir"),
                "size": int(mt.group("size")), "strategy": mt.group("strat").strip(),
                "outcome_raw": mt.group("outcome").strip(),
                "outcome": classify_outcome(mt.group("outcome")),
                "pnl": round(pnl, 2), "sl_pts": sl, "tp_pts": tp,
                "planned_rr": rr, "source": source, "tf": tf, "ifvg": ifvg,
                "aplus": aplus,
            })
    return trades

def main():
    trades = parse()
    if not trades:
        print("No trades parsed — check results.txt format.", file=sys.stderr)
        sys.exit(1)
    generated = datetime.datetime.now().strftime("%a %d %b %Y, %I:%M %p")
    payload = {"generated": generated, "trades": trades}
    with open(TPL, encoding="utf-8") as f:
        template = f.read()
    html = template.replace("/*__DATA__*/", json.dumps(payload))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    net = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    print(f"Wrote {OUT}")
    print(f"{len(trades)} trades · net ${net:,.2f} · {wins} winners "
          f"({wins/len(trades)*100:.1f}% win rate)")

if __name__ == "__main__":
    main()
