"""Trading Coach MCP server (stdio, JSON-RPC 2.0, stdlib only).

Paper trading only: market data comes from Zerodha Kite (read-only); no orders are ever sent.
"""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from coach import analytics, behavior, kite, paper, store  # noqa: E402
import watcher  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
TOOLS = {}


def tool(name, description, properties=None, required=()):
    def register(fn):
        TOOLS[name] = {
            "fn": fn,
            "spec": {"name": name, "description": description,
                     "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)}},
        }
        return fn
    return register


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}


# ---------------------------------------------------------------- status & login

@tool("coach_status", "Snapshot: Kite login state, price source, open paper positions with live P&L, today's discipline stats, watcher state. Call at the start of every coaching interaction.")
def coach_status():
    book = store.load_book()
    st = behavior.day_stats(book)
    live = store.read_json("live.json", {})
    positions = paper.positions_with_pnl(book)
    unrealized = sum(p["unrealized_pnl"] or 0 for p in positions)
    return {
        "time_ist": store.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kite": kite.session_status(),
        "price_source": store.load_config()["price_source"],
        "open_positions": positions,
        "today": {"trades_taken": st["trades_taken"], "realized_pnl": st["realized_pnl"],
                  "unrealized_pnl": round(unrealized, 2), "day_pnl": round(st["realized_pnl"] + unrealized, 2),
                  "wins": st["wins"], "losses": st["losses"], "consecutive_losses": st["consecutive_losses"]},
        "watcher": {"running": watcher.is_running(), "last_update": live.get("updated_at")},
        "data_dir": str(store.HOME),
    }


@tool("kite_login", "Start Zerodha Kite login. Returns a URL for the TRADER to open and log in themselves. A local listener on 127.0.0.1 captures the redirect and completes login automatically. Never ask for or handle the trader's Zerodha password.")
def kite_login():
    url = kite.login_url()
    port = store.load_config()["kite_redirect_port"]
    try:
        kite.start_callback_server(timeout=300)
        listening = True
    except OSError:
        listening = False
    return {
        "login_url": url,
        "listening_for_redirect": listening,
        "redirect_url_expected": f"http://127.0.0.1:{port}/kite/callback",
        "instructions": "Open login_url, log in on Zerodha's page. If it does not finish automatically, "
                        "copy the full URL the browser ends on and pass it to kite_complete_login.",
    }


@tool("kite_complete_login", "Finish Kite login manually with the request_token (or the full redirect URL containing it).",
      {"request_token_or_url": S}, ["request_token_or_url"])
def kite_complete_login(request_token_or_url):
    return kite.exchange_request_token(kite.parse_request_token(request_token_or_url))


# ---------------------------------------------------------------- rules

@tool("get_rules", "Return the trader's numeric limits and yes/no checklist rules.")
def get_rules():
    return store.load_rules()


@tool("set_limit", "Set one numeric/boolean limit, e.g. max_trades_per_day=3, max_daily_loss=5000, trading_window=['09:30','15:00'].",
      {"key": S, "value": {}}, ["key", "value"])
def set_limit(key, value):
    rules = store.load_rules()
    if key not in store.DEFAULT_RULES["limits"]:
        raise paper.CoachError(f"Unknown limit {key}. Known: {', '.join(store.DEFAULT_RULES['limits'])}")
    rules["limits"][key] = value
    store.save_rules(rules)
    return rules["limits"]


@tool("add_checklist_rule", "Add a yes/no rule the trader must confirm before every entry (phrase it so YES = rule followed).",
      {"text": S}, ["text"])
def add_checklist_rule(text):
    rules = store.load_rules()
    used = {c["id"] for c in rules["checklist"]}
    n = 1
    while f"c{n}" in used:
        n += 1
    rules["checklist"].append({"id": f"c{n}", "text": text})
    store.save_rules(rules)
    return rules["checklist"]


@tool("remove_checklist_rule", "Remove a checklist rule by id.", {"id": S}, ["id"])
def remove_checklist_rule(id):
    rules = store.load_rules()
    rules["checklist"] = [c for c in rules["checklist"] if c["id"] != id]
    store.save_rules(rules)
    return rules["checklist"]


# ---------------------------------------------------------------- trading style & setups

@tool("get_profile", "The trader's saved trading style (type, instruments, timeframes, holding time, session, strengths, weaknesses) and setup playbook.")
def get_profile():
    return store.load_profile()


@tool("update_profile", "Save or update trading-style fields in the trader's own words. Keys: trader_type, instruments (list), timeframes (list), typical_holding, session, capital, strengths, weaknesses, notes.",
      {"style": {"type": "object"}}, ["style"])
def update_profile(style):
    profile = store.load_profile()
    unknown = set(style) - set(store.DEFAULT_PROFILE["style"])
    if unknown:
        raise paper.CoachError(f"Unknown style fields: {', '.join(unknown)}")
    profile["style"].update(style)
    store.save_profile(profile)
    return profile["style"]


@tool("save_setup", "Add or replace a named setup in the trader's playbook, e.g. name='ORB', criteria=['15m opening range break', 'volume above average'].",
      {"name": S, "description": S, "criteria": {"type": "array", "items": S}, "timeframe": S}, ["name"])
def save_setup(name, description="", criteria=None, timeframe=""):
    profile = store.load_profile()
    profile["setups"] = [x for x in profile["setups"] if x["name"].lower() != name.lower()]
    profile["setups"].append({"name": name, "description": description, "criteria": criteria or [], "timeframe": timeframe})
    store.save_profile(profile)
    return profile["setups"]


@tool("remove_setup", "Remove a setup from the playbook by name.", {"name": S}, ["name"])
def remove_setup(name):
    profile = store.load_profile()
    profile["setups"] = [x for x in profile["setups"] if x["name"].lower() != name.lower()]
    store.save_profile(profile)
    return profile["setups"]


@tool("style_analytics", "Analytics on the trader's style over N days (default 30): summary, observed style, P&L by setup, time of day, weekday, emotion, holding time, exit reason, side, rules followed vs broken, trades right after a loss, equity curve, and plain-language insights.",
      {"days": I})
def style_analytics(days=30):
    r = analytics.compute(store.load_book(), store.load_profile(), days)
    r.pop("equity_curve", None)
    return r


@tool("style_report", "Write a visual HTML trading-style report (charts + setup scorecard) for the last N days and return its file path.",
      {"days": I})
def style_report(days=30):
    return analytics.write_report(store.load_book(), store.load_profile(), days)


# ---------------------------------------------------------------- market data

@tool("resolve_option", "Resolve a trade idea like 'NIFTY ATM CE' into a concrete contract with live spot and option LTP from Kite.",
      {"underlying": {"type": "string", "description": "NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX"},
       "option_type": {"type": "string", "enum": ["CE", "PE"]},
       "strike": {"type": "string", "description": "ATM, ATM+1, ATM-2 (strike steps), or a number like 24500"},
       "expiry": {"type": "string", "description": "nearest (default), next, or YYYY-MM-DD"}},
      ["underlying", "option_type"])
def resolve_option(underlying, option_type, strike="ATM", expiry="nearest"):
    return kite.resolve_option(underlying, option_type, strike, expiry)


@tool("get_quote", "Live last traded prices from Kite, e.g. ['NSE:NIFTY 50', 'NFO:NIFTY25O2824500CE'].",
      {"symbols": {"type": "array", "items": S}}, ["symbols"])
def get_quote(symbols):
    return kite.ltp(symbols)


# ---------------------------------------------------------------- paper trading

PROPOSAL = {
    "underlying": S, "option_type": S, "strike": S, "expiry": S,
    "side": {"type": "string", "enum": ["BUY", "SELL"]},
    "lots": I,
    "stop_loss": {"type": "number", "description": "Option premium price level, not points or %"},
    "target": {"type": "number", "description": "Option premium price level"},
    "reason": {"type": "string", "description": "The trader's own words for why they want this trade"},
    "price": {"type": "number", "description": "Only for price_source=manual: current option price"},
    "lot_size": {"type": "integer", "description": "Only for price_source=manual"},
    "setup": {"type": "string", "description": "Name of the setup from the trader's playbook (see get_profile)"},
    "timeframe": {"type": "string", "description": "Chart timeframe the setup was seen on, e.g. 5m"},
}


def _contract(underlying, option_type, strike="ATM", expiry="nearest", price=None, lot_size=None):
    if store.load_config()["price_source"] == "manual":
        if price is None or lot_size is None:
            raise paper.CoachError("Manual price mode: ask the trader for the option's current price and lot size.")
        name = f"{underlying.upper()} {expiry} {strike} {option_type.upper()}"
        return {"underlying": underlying.upper(), "option_type": option_type.upper(), "strike": strike,
                "expiry": expiry, "tradingsymbol": name, "symbol": f"MANUAL:{name}", "lot_size": lot_size,
                "ltp": price, "tick_size": 0.05}
    return kite.resolve_option(underlying, option_type, strike, expiry)


@tool("pre_trade_check", "Run all discipline checks on a proposed trade BEFORE entry. Returns flags (BLOCK/WARN), today's stats and the checklist to ask. Does not enter anything.",
      PROPOSAL, ["underlying", "option_type", "side", "lots"])
def pre_trade_check(underlying, option_type, side, lots, strike="ATM", expiry="nearest",
                    stop_loss=None, target=None, reason="", price=None, lot_size=None, setup=None, timeframe=None):
    c = _contract(underlying, option_type, strike, expiry, price, lot_size)
    result = paper.check({"underlying": c["underlying"], "option_type": c["option_type"],
                          "tradingsymbol": c["tradingsymbol"], "side": side.upper(), "lots": lots,
                          "lot_size": c["lot_size"], "price": c["ltp"], "stop_loss": stop_loss,
                          "target": target, "reason": reason, "setup": setup})
    result["contract"] = c
    return result


@tool("paper_enter", "Enter a PAPER trade at the live price (+slippage). Requires every checklist id answered true/false. If pre-trade checks BLOCK, it is refused unless override_reason is given in the trader's own words.",
      {**PROPOSAL,
       "checklist_answers": {"type": "object", "description": "{checklist_id: true|false} from the trader's answers"},
       "emotion": {"type": "string", "description": "Trader's self-reported state: calm, confident, anxious, fomo, revenge, bored, euphoric..."},
       "override_reason": {"type": "string", "description": "Only if the trader insists on breaking a BLOCK rule; their words"}},
      ["underlying", "option_type", "side", "lots", "checklist_answers"])
def paper_enter(underlying, option_type, side, lots, checklist_answers, strike="ATM", expiry="nearest",
                stop_loss=None, target=None, reason="", emotion=None, override_reason=None,
                price=None, lot_size=None, setup=None, timeframe=None):
    c = _contract(underlying, option_type, strike, expiry, price, lot_size)
    result = paper.enter(c, side, lots, stop_loss, target, reason, checklist_answers, emotion,
                         override_reason, price=price, setup=setup, timeframe=timeframe)
    if result.get("entered"):
        result["watcher"] = _ensure_watcher()
    return result


@tool("paper_exit", "Close an open paper position (id) or 'all' at the live price.",
      {"trade_id": S, "reason": {"type": "string", "description": "manual, plan_exit, fear, panic, time_stop..."},
       "note": S, "price": {"type": "number", "description": "Only for price_source=manual"}})
def paper_exit(trade_id="all", reason="manual", note=None, price=None):
    return paper.exit_trades(trade_id, reason, note, price)


@tool("journal", "List paper trades for a date (YYYY-MM-DD, default today) with flags, answers and P&L.",
      {"date": S})
def journal(date=None):
    day = date or store.today_str()
    return [t for t in store.load_book()["trades"] if t["opened_at"][:10] == day]


@tool("review", "Discipline review over the last N days: P&L when rules followed vs broken, flag frequency, exits, P&L by emotion.",
      {"days": I})
def review(days=5):
    return behavior.review(store.load_book(), days)


# ---------------------------------------------------------------- live P&L watcher

def _ensure_watcher():
    if watcher.is_running():
        return {"running": True, "already": True, "follow_command": _follow_cmd()}
    out = open(store.path("watcher.out"), "a", encoding="utf-8")
    kwargs = {"stdout": out, "stderr": out, "stdin": subprocess.DEVNULL, "cwd": str(HERE)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(HERE / "watcher.py")], **kwargs)
    return {"running": True, "started": True, "follow_command": _follow_cmd()}


def _follow_cmd():
    return f'"{sys.executable}" "{HERE / "watcher.py"}" --follow-events'


@tool("watcher_start", "Start the background live P&L watcher (auto SL/target exits, daily-loss warnings). Returns follow_command: in Claude Code, run it with the Monitor tool so events stream into the chat.")
def watcher_start():
    return _ensure_watcher()


@tool("watcher_stop", "Stop the background live P&L watcher.")
def watcher_stop():
    store.path("watcher.stop").touch()
    return {"stopping": True}


@tool("live_pnl", "Latest P&L snapshot written by the watcher (falls back to fetching prices directly).")
def live_pnl():
    if watcher.is_running():
        return store.read_json("live.json", {})
    return coach_status()


@tool("recent_events", "Coach events (entries, exits, SL/target hits, loss warnings) since an ISO timestamp, or the last 20.",
      {"since": S})
def recent_events(since=None):
    log = store.path("events.log")
    if not log.exists():
        return []
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if e["ts"] > since] if since else events[-20:]


# ---------------------------------------------------------------- JSON-RPC plumbing

def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "trading-coach", "version": "0.1.0"},
            "instructions": "Paper-trading discipline coach for discretionary F&O traders. Never places real orders.",
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [t["spec"] for t in TOOLS.values()]}}
    if method == "tools/call":
        params = msg.get("params", {})
        entry = TOOLS.get(params.get("name"))
        if not entry:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {params.get('name')}"}}
        try:
            out = entry["fn"](**(params.get("arguments") or {}))
            text, is_error = json.dumps(out, indent=1, ensure_ascii=False, default=str), False
        except (paper.CoachError, kite.KiteError, TimeoutError, TypeError, ValueError) as e:
            text, is_error = f"{type(e).__name__}: {e}", True
        except Exception:
            text, is_error = traceback.format_exc(limit=3), True
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None  # notification
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
