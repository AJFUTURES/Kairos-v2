"""KAIROS adaptive position-sizing engine (micros only).

Pure, side-effect-free ladder logic, kept OUT of main.py so it can be unit-tested
off the bot. main.py owns persistence, order placement and the dashboard; this
module only decides how the risk multiplier and the daily-loss stop evolve as
trades close.

Model (all values tunable via settings):
  * Base = 1 micro. Contracts traded = max(1, round(base_micros * mult)). No cap.
  * Every WIN       -> mult *= win_growth (default 1.20); consecutive-loss run resets to 0.
  * Every LOSS      -> loss_run += 1; on the `cut_after`-th loss IN A ROW (default 2)
                       mult *= loss_cut (default 0.50, floored at floor_mult); run resets.
  * BREAK-EVEN      -> |net| <= be_band: no change at all. The trade does not count
                       (no growth, no cut, streak + daily counter untouched).
  * The multiplier CARRIES across days (the caller persists `state`).
  * Daily stop      -> after `daily_loss_limit` losing trades in one futures day,
                       state["stopped"] = True. Manual resume only (caller clears it).
                       The daily loss COUNT resets at each new futures day; the
                       `stopped` flag does not (manual resume only).

Per-instrument fixed geometry (stop/target in points) also lives in settings, but
this module doesn't need it to move the ladder — the caller reads it when it
builds the order. Everything here is deterministic and has no I/O.
"""

from copy import deepcopy

# Only these micros are eligible for adaptive mode.
MICROS = ("MNQ", "MES", "MGC")

DEFAULT_SETTINGS = {
    "win_growth":       1.20,   # x per win
    "loss_cut":         0.50,   # x on the cut
    "cut_after":        2,      # cut on every Nth consecutive loss
    "floor_mult":       0.50,   # multiplier never falls below this
    "base_micros":      1,      # contracts the ladder starts from
    "be_band":          25.0,   # |net $| <= this => break-even, doesn't count
    "daily_loss_limit": 10,     # losing trades in a day before the manual-resume stop
    "instruments": {            # fixed per-instrument geometry (points)
        "MNQ": {"stop_pts": 20.0, "tp_pts": 50.0},   # 2.5:1
        "MES": {"stop_pts": 6.0,  "tp_pts": 6.0},    # 1:1
        "MGC": {"stop_pts": 6.0,  "tp_pts": 6.0},    # 1:1
    },
}


def default_settings() -> dict:
    return deepcopy(DEFAULT_SETTINGS)


def default_state() -> dict:
    return {
        "mult":          1.0,   # continuous risk multiplier (base = 1.0 = base_micros)
        "loss_run":      0,     # consecutive losses since the last win (toward cut_after)
        "daily_losses":  0,     # losing trades counted in the current futures day
        "day_key":       "",    # futures-day label the daily count belongs to
        "stopped":       False, # daily-loss stop tripped (manual resume only)
        "last_ts":       "",    # broker timestamp of the last close already applied
    }


def _f(v, dflt):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(dflt)


def sanitize_settings(raw: dict) -> dict:
    """Validate/clamp a settings dict coming from the dashboard, filling any gap
    from DEFAULT_SETTINGS. Never raises — bad fields fall back to the default."""
    d = default_settings()
    if not isinstance(raw, dict):
        return d
    d["win_growth"]       = min(3.0,  max(1.0, _f(raw.get("win_growth"),       d["win_growth"])))
    d["loss_cut"]         = min(1.0,  max(0.05, _f(raw.get("loss_cut"),        d["loss_cut"])))
    d["floor_mult"]       = min(1.0,  max(0.01, _f(raw.get("floor_mult"),      d["floor_mult"])))
    d["base_micros"]      = max(1, int(_f(raw.get("base_micros"),      d["base_micros"])))
    d["cut_after"]        = max(1, int(_f(raw.get("cut_after"),        d["cut_after"])))
    d["daily_loss_limit"] = max(1, int(_f(raw.get("daily_loss_limit"), d["daily_loss_limit"])))
    d["be_band"]          = max(0.0, _f(raw.get("be_band"),           d["be_band"]))
    inst = raw.get("instruments")
    if isinstance(inst, dict):
        for sym in MICROS:
            row = inst.get(sym)
            if isinstance(row, dict):
                d["instruments"][sym] = {
                    "stop_pts": max(0.25, _f(row.get("stop_pts"), d["instruments"][sym]["stop_pts"])),
                    "tp_pts":   max(0.25, _f(row.get("tp_pts"),   d["instruments"][sym]["tp_pts"])),
                }
    return d


def classify(net: float, be_band: float) -> str:
    """'neutral' (break-even, doesn't count), 'win', or 'loss'."""
    try:
        net = float(net)
    except (TypeError, ValueError):
        return "neutral"
    if abs(net) <= float(be_band):
        return "neutral"
    return "win" if net > 0 else "loss"


def contracts_for(mult: float, base_micros: int) -> int:
    """Whole micros to trade at the current multiplier. Floored at 1 micro. No cap."""
    try:
        n = round(float(mult) * int(base_micros))
    except (TypeError, ValueError):
        n = base_micros
    return max(1, int(n))


def geometry_for(settings: dict, symbol: str):
    """(stop_pts, tp_pts) for a micro, or None if the symbol isn't an adaptive micro."""
    row = (settings or {}).get("instruments", {}).get(symbol)
    if not row:
        return None
    return float(row.get("stop_pts", 0)), float(row.get("tp_pts", 0))


def apply_close(state: dict, settings: dict, net: float, day_key: str) -> dict:
    """Advance the ladder for ONE closed trade. Mutates `state` in place and returns
    an events dict describing what happened (for logging/notifications):
        {kind, grew, cut, stop_tripped, mult_before, mult_after}
    The caller is responsible for only calling this ONCE per real close (e.g. guard
    on the broker trade timestamp vs state['last_ts'])."""
    ev = {"kind": "neutral", "grew": False, "cut": False, "stop_tripped": False,
          "mult_before": state.get("mult", 1.0), "mult_after": state.get("mult", 1.0)}

    # New futures day -> reset the daily loss COUNT (the stop flag persists until a
    # manual resume, by design).
    if day_key and day_key != state.get("day_key"):
        state["day_key"] = day_key
        state["daily_losses"] = 0

    kind = classify(net, settings.get("be_band", 0.0))
    ev["kind"] = kind
    if kind == "neutral":
        ev["mult_after"] = state.get("mult", 1.0)
        return ev

    if kind == "win":
        state["mult"] = float(state.get("mult", 1.0)) * float(settings.get("win_growth", 1.2))
        state["loss_run"] = 0
        ev["grew"] = True
    else:  # loss
        state["loss_run"] = int(state.get("loss_run", 0)) + 1
        state["daily_losses"] = int(state.get("daily_losses", 0)) + 1
        if state["loss_run"] >= int(settings.get("cut_after", 2)):
            floor = float(settings.get("floor_mult", 0.5))
            state["mult"] = max(float(state.get("mult", 1.0)) * float(settings.get("loss_cut", 0.5)), floor)
            state["loss_run"] = 0
            ev["cut"] = True
        if (not state.get("stopped")) and state["daily_losses"] >= int(settings.get("daily_loss_limit", 10)):
            state["stopped"] = True
            ev["stop_tripped"] = True

    ev["mult_after"] = state.get("mult", 1.0)
    return ev
