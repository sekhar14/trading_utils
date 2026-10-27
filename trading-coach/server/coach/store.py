"""Local storage for the trading coach: config, rules, paper trades, events.

Everything lives in ~/.trading-coach (override with TRADING_COACH_HOME).
Writes are atomic and guarded by a lock file because the MCP server and the
P&L watcher run as separate processes.
"""
import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
HOME = Path(os.environ.get("TRADING_COACH_HOME") or Path.home() / ".trading-coach")

DEFAULT_CONFIG = {
    "price_source": "kite",          # "kite" or "manual"
    "kite_api_key": "",
    "kite_api_secret": "",
    "kite_redirect_port": 5678,       # redirect URL: http://127.0.0.1:5678/kite/callback
    "slippage_ticks": 1,              # paper fills: buy at ltp + n ticks, sell at ltp - n ticks
}

DEFAULT_RULES = {
    "limits": {
        "max_trades_per_day": 3,
        "max_daily_loss": 5000,
        "max_consecutive_losses": 2,
        "cooldown_minutes_after_loss": 15,
        "max_lots_per_trade": 2,
        "max_open_positions": 1,
        "max_risk_per_trade": 2000,
        "min_reward_risk": 1.5,
        "trading_window": ["09:30", "15:00"],
        "require_stop_loss": True,
        "no_size_up_after_loss": True,
        "auto_square_off_on_daily_loss": True,
    },
    "checklist": [
        {"id": "c1", "text": "Is this trade part of my written plan for today (not a spur-of-the-moment idea)?"},
        {"id": "c2", "text": "Is the trade in the direction of the 15-minute trend?"},
        {"id": "c3", "text": "Am I calm and not trying to recover an earlier loss?"},
    ],
}

DEFAULT_PROFILE = {
    "style": {
        "trader_type": "",          # e.g. "intraday option buyer", "scalper", "positional option seller"
        "instruments": [],          # e.g. ["NIFTY", "BANKNIFTY"]
        "timeframes": [],           # charts used for decisions, e.g. ["5m", "15m"]
        "typical_holding": "",      # e.g. "10-40 min"
        "session": "",              # e.g. "first 2 hours only"
        "capital": None,
        "strengths": "",
        "weaknesses": "",           # what the trader says goes wrong, e.g. "revenge trades after first loss"
        "notes": "",
    },
    "setups": [],                   # [{"name", "description", "criteria": [...], "timeframe"}]
}


def now():
    return datetime.now(IST)


def today_str():
    return now().strftime("%Y-%m-%d")


def path(name):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name


def read_json(name, default):
    p = path(name)
    if not p.exists():
        return json.loads(json.dumps(default))
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(name, data):
    p = path(name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


@contextlib.contextmanager
def locked(timeout=10.0, stale_after=30.0):
    """Cross-process lock around read-modify-write of the trade book."""
    lock = path("store.lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise TimeoutError("trading-coach store is locked by another process")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json("config.json", {}))
    cfg["kite_api_key"] = os.environ.get("KITE_API_KEY") or cfg["kite_api_key"]
    cfg["kite_api_secret"] = os.environ.get("KITE_API_SECRET") or cfg["kite_api_secret"]
    return cfg


def save_config(updates):
    cfg = read_json("config.json", {})
    cfg.update(updates)
    write_json("config.json", cfg)


def load_rules():
    rules = read_json("rules.json", DEFAULT_RULES)
    limits = dict(DEFAULT_RULES["limits"])
    limits.update(rules.get("limits", {}))
    rules["limits"] = limits
    rules.setdefault("checklist", [])
    return rules


def save_rules(rules):
    write_json("rules.json", rules)


def load_profile():
    profile = read_json("profile.json", DEFAULT_PROFILE)
    style = dict(DEFAULT_PROFILE["style"])
    style.update(profile.get("style", {}))
    profile["style"] = style
    profile.setdefault("setups", [])
    return profile


def save_profile(profile):
    write_json("profile.json", profile)


def load_book():
    return read_json("trades.json", {"trades": []})


def save_book(book):
    write_json("trades.json", book)


def append_event(kind, message, **data):
    event = {"ts": now().isoformat(timespec="seconds"), "kind": kind, "message": message, **data}
    with open(path("events.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event
