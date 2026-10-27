"""Deterministic discipline checks. The LLM coaches; this code decides the facts.

Severity:
  BLOCK - a hard rule the trader set is broken. Entry needs an explicit override reason.
  WARN  - a behavioural red flag (revenge, FOMO, overtrading). Coach must raise it.
"""
import re
from datetime import datetime, timedelta

from . import store

EMOTION_WORDS = [
    "recover", "recoup", "make back", "get back", "get it back", "revenge", "missed", "miss out", "fomo",
    "sure shot", "sureshot", "100%", "guaranteed", "can't lose", "cant lose", "must", "quick",
    "quickly", "jackpot", "all in", "double", "frustrated", "angry", "tilt", "bored", "one more",
    "last trade", "just this once", "running away", "rocket", "pakka", "jaldi",
]


def _dt(s):
    return datetime.fromisoformat(s)


def day_stats(book, day=None):
    day = day or store.today_str()
    trades = [t for t in book["trades"] if t["opened_at"][:10] == day]
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["closed_at"])
    realized = sum(t["pnl"] for t in closed)
    consec = 0
    for t in reversed(closed):
        if t["pnl"] < 0:
            consec += 1
        else:
            break
    last_loss = next((t for t in reversed(closed) if t["pnl"] < 0), None)
    return {
        "date": day,
        "trades_taken": len(trades),
        "open_positions": [t for t in trades if t["status"] == "open"],
        "closed": closed,
        "realized_pnl": round(realized, 2),
        "wins": sum(1 for t in closed if t["pnl"] > 0),
        "losses": sum(1 for t in closed if t["pnl"] < 0),
        "consecutive_losses": consec,
        "last_loss": last_loss,
        "last_trade": max(trades, key=lambda t: t["opened_at"]) if trades else None,
    }


def emotional_words(text):
    text = (text or "").lower()
    return [w for w in EMOTION_WORDS if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text)]


def pre_trade_check(book, rules, proposal, unrealized_pnl=0.0, profile=None):
    """proposal: underlying, option_type, tradingsymbol, side, lots, lot_size, price, stop_loss, target, reason"""
    L = rules["limits"]
    st = day_stats(book)
    t_now = store.now()
    flags = []

    def flag(code, severity, message):
        flags.append({"code": code, "severity": severity, "message": message})

    if st["trades_taken"] >= L["max_trades_per_day"]:
        flag("MAX_TRADES", "BLOCK", f"Already {st['trades_taken']} trades today; your limit is {L['max_trades_per_day']}.")

    day_pnl = st["realized_pnl"] + unrealized_pnl
    if day_pnl <= -abs(L["max_daily_loss"]):
        flag("DAILY_LOSS", "BLOCK", f"Day P&L {day_pnl:,.0f} has hit your max daily loss of {L['max_daily_loss']:,}. Day is over.")

    if st["consecutive_losses"] >= L["max_consecutive_losses"]:
        flag("CONSEC_LOSSES", "BLOCK", f"{st['consecutive_losses']} losses in a row; your rule says stop at {L['max_consecutive_losses']}.")

    if st["last_loss"]:
        since = (t_now - _dt(st["last_loss"]["closed_at"])).total_seconds() / 60
        if since < L["cooldown_minutes_after_loss"]:
            flag("COOLDOWN", "WARN", f"Last loss was {since:.0f} min ago (cooldown {L['cooldown_minutes_after_loss']} min). Classic revenge-trade window.")
        last_loss = st["last_loss"]
        if since < 10 and last_loss["underlying"] == proposal.get("underlying") and last_loss["option_type"] == proposal.get("option_type"):
            flag("REENTRY", "WARN", f"Re-entering the same {last_loss['underlying']} {last_loss['option_type']} side {since:.0f} min after a loss on it.")

    lots = int(proposal.get("lots") or 0)
    if lots > L["max_lots_per_trade"]:
        flag("MAX_LOTS", "BLOCK", f"{lots} lots exceeds your max of {L['max_lots_per_trade']}.")

    last = st["last_trade"]
    if L["no_size_up_after_loss"] and st["last_loss"] and last and lots > int(last["lots"]):
        flag("SIZE_UP_AFTER_LOSS", "WARN", f"Sizing up from {last['lots']} to {lots} lots after a loss (martingale behaviour).")

    if len(st["open_positions"]) >= L["max_open_positions"]:
        flag("MAX_OPEN", "BLOCK", f"{len(st['open_positions'])} position(s) already open; limit is {L['max_open_positions']}.")

    start, end = L["trading_window"]
    hhmm = t_now.strftime("%H:%M")
    if not (start <= hhmm <= end):
        flag("OUTSIDE_WINDOW", "BLOCK", f"It is {hhmm}; your trading window is {start}-{end}.")

    recent = [t for t in book["trades"] if (t_now - _dt(t["opened_at"])) <= timedelta(minutes=30)]
    if len(recent) >= 2:
        flag("RAPID_FIRE", "WARN", f"{len(recent)} trades in the last 30 minutes. Overtrading pattern.")

    price = proposal.get("price")
    sl = proposal.get("stop_loss")
    tgt = proposal.get("target")
    qty = lots * int(proposal.get("lot_size") or 0)
    if sl in (None, "", 0):
        if L["require_stop_loss"]:
            flag("NO_STOP_LOSS", "BLOCK", "No stop-loss given. Your rules require one before entry.")
    elif price:
        risk = abs(price - float(sl)) * qty
        if risk > L["max_risk_per_trade"]:
            flag("MAX_RISK", "BLOCK", f"Risk to SL is {risk:,.0f}; your max risk per trade is {L['max_risk_per_trade']:,}.")
        if tgt not in (None, "", 0) and abs(price - float(sl)) > 0:
            rr = abs(float(tgt) - price) / abs(price - float(sl))
            if rr < L["min_reward_risk"]:
                flag("LOW_RR", "WARN", f"Reward:risk is {rr:.2f}; your minimum is {L['min_reward_risk']}.")

    words = emotional_words(proposal.get("reason"))
    if words:
        flag("EMOTIONAL_LANGUAGE", "WARN", f"Your reason contains emotionally loaded words: {', '.join(words)}.")
    if not (proposal.get("reason") or "").strip():
        flag("NO_REASON", "WARN", "No reason given for the trade. Every trade needs a written why.")

    setups = {x["name"].lower(): x for x in (profile or {}).get("setups", [])}
    setup = (proposal.get("setup") or "").strip()
    playbook = None
    if not setup:
        flag("NO_SETUP", "WARN", "No setup named. Which setup from your playbook is this?")
    elif setups and setup.lower() not in setups:
        flag("OFF_PLAYBOOK", "WARN", f"'{setup}' is not one of your saved setups ({', '.join(x['name'] for x in setups.values())}).")
    else:
        playbook = setups.get(setup.lower())
    instruments = [i.upper() for i in (profile or {}).get("style", {}).get("instruments", [])]
    if instruments and proposal.get("underlying") and proposal["underlying"].upper() not in instruments:
        flag("OFF_STYLE_INSTRUMENT", "WARN", f"You usually trade {', '.join(instruments)}, not {proposal['underlying']}.")

    summary = {k: v for k, v in st.items() if k not in ("open_positions", "closed", "last_loss", "last_trade")}
    summary["open_positions"] = len(st["open_positions"])
    summary["unrealized_pnl"] = round(unrealized_pnl, 2)
    return {
        "flags": flags,
        "blocked": any(f["severity"] == "BLOCK" for f in flags),
        "today": summary,
        "checklist": rules["checklist"],
        "setup_criteria": playbook,
    }


def review(book, days=5):
    since = (store.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    closed = [t for t in book["trades"] if t["status"] == "closed" and t["opened_at"][:10] >= since]
    by_day = {}
    for t in closed:
        d = by_day.setdefault(t["opened_at"][:10], {"trades": 0, "pnl": 0.0, "wins": 0, "rule_breaks": 0})
        d["trades"] += 1
        d["pnl"] = round(d["pnl"] + t["pnl"], 2)
        d["wins"] += t["pnl"] > 0
        d["rule_breaks"] += not t.get("rules_followed", True)

    def bucket(ts):
        n = len(ts)
        return {"trades": n, "pnl": round(sum(t["pnl"] for t in ts), 2),
                "win_rate": round(100 * sum(t["pnl"] > 0 for t in ts) / n, 1) if n else None}

    flag_counts = {}
    for t in closed:
        for f in t.get("flags", []):
            flag_counts[f["code"]] = flag_counts.get(f["code"], 0) + 1
    exits = {}
    for t in closed:
        exits[t.get("exit_reason", "manual")] = exits.get(t.get("exit_reason", "manual"), 0) + 1
    emotions = {}
    for t in closed:
        if t.get("emotion"):
            emotions.setdefault(t["emotion"], []).append(t["pnl"])

    return {
        "period_start": since,
        "overall": bucket(closed),
        "followed_rules": bucket([t for t in closed if t.get("rules_followed", True)]),
        "broke_rules": bucket([t for t in closed if not t.get("rules_followed", True)]),
        "overrides": sum(1 for t in closed if t.get("override_reason")),
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: -kv[1])),
        "exit_reasons": exits,
        "pnl_by_emotion": {k: {"trades": len(v), "pnl": round(sum(v), 2)} for k, v in emotions.items()},
        "by_day": by_day,
    }
