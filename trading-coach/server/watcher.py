"""Live P&L watcher for paper positions.

    python watcher.py                  run the watcher (started detached by the MCP server)
    python watcher.py --follow-events  print new coach events as they happen (for Claude Code's Monitor)

Every poll it fetches LTPs for open paper positions, closes them when the stop-loss
or target is touched, warns as the day's loss approaches the limit, and writes
live.json (current P&L snapshot) plus events.log (one JSON line per event).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coach import behavior, kite, paper, store  # noqa: E402

HEARTBEAT_STALE_SECONDS = 20


def is_running():
    live = store.read_json("live.json", {})
    return bool(live.get("heartbeat")) and time.time() - live["heartbeat"] < HEARTBEAT_STALE_SECONDS


def market_open():
    t = store.now()
    return t.weekday() < 5 and "09:15" <= t.strftime("%H:%M") <= "15:30"


def _emit(kind, message, **data):
    event = store.append_event(kind, message, **data)
    print(f"[{event['ts'][11:19]}] {kind}: {message}", flush=True)


def _hit(trade, ltp):
    long = trade["side"] == "BUY"
    if trade.get("stop_loss") is not None and (ltp <= trade["stop_loss"] if long else ltp >= trade["stop_loss"]):
        return "stop_loss", trade["stop_loss"]
    if trade.get("target") is not None and (ltp >= trade["target"] if long else ltp <= trade["target"]):
        return "target", trade["target"]
    return None, None


def poll_once(state):
    book = store.load_book()
    open_trades = [t for t in book["trades"] if t["status"] == "open"]
    rules = store.load_rules()
    max_loss = abs(rules["limits"]["max_daily_loss"])
    prices = paper.live_prices([t["symbol"] for t in open_trades]) if open_trades else {}

    # SL / target: fill at the trigger level, or at LTP if price gapped through it.
    hits = []
    for t in open_trades:
        ltp = prices.get(t["symbol"])
        if ltp is None:
            continue
        kind, level = _hit(t, ltp)
        if kind:
            long = t["side"] == "BUY"
            fill = min(level, ltp) if (kind == "stop_loss") == long else max(level, ltp)
            hits.append((t["id"], kind, fill))
    if hits:
        with store.locked():
            book = store.load_book()
            by_id = {t["id"]: t for t in book["trades"]}
            for tid, kind, fill in hits:
                t = by_id[tid]
                if t["status"] == "open":
                    paper.close_trade(book, t, fill, kind)
                    label = "SL_HIT" if kind == "stop_loss" else "TARGET_HIT"
                    _emit(label, f"{tid} {t['tradingsymbol']} closed @ {fill} | P&L {t['pnl']:,.0f}",
                          trade_id=tid, pnl=t["pnl"])
            store.save_book(book)
        open_trades = [t for t in book["trades"] if t["status"] == "open"]

    st = behavior.day_stats(book)
    positions = paper.positions_with_pnl(book, prices)
    unrealized = sum(p["unrealized_pnl"] or 0 for p in positions)
    day_pnl = st["realized_pnl"] + unrealized

    # Loss-limit warnings, once per level per day.
    fired = state.setdefault(st["date"], set())
    for pct in (50, 80, 100):
        if day_pnl <= -max_loss * pct / 100 and pct not in fired:
            fired.add(pct)
            if pct < 100:
                _emit("LOSS_WARNING", f"Day P&L {day_pnl:,.0f} is {pct}% of your max daily loss ({max_loss:,}).",
                      day_pnl=day_pnl)
            else:
                _emit("DAILY_LOSS_LIMIT", f"Day P&L {day_pnl:,.0f} hit max daily loss ({max_loss:,}). Stop trading for today.",
                      day_pnl=day_pnl)
                if rules["limits"].get("auto_square_off_on_daily_loss") and open_trades:
                    result = paper.exit_trades("all", reason="daily_loss_limit")
                    _emit("SQUARED_OFF", f"Closed {len(result['closed'])} position(s). Day P&L {result['day_realized_pnl']:,.0f}")

    store.write_json("live.json", {
        "heartbeat": time.time(),
        "updated_at": store.now().isoformat(timespec="seconds"),
        "positions": positions,
        "realized_pnl": st["realized_pnl"],
        "unrealized_pnl": round(unrealized, 2),
        "day_pnl": round(day_pnl, 2),
    })
    return bool(open_trades)


def run():
    if is_running():
        print("watcher already running", flush=True)
        return
    stop_flag = store.path("watcher.stop")
    stop_flag.unlink(missing_ok=True)
    _emit("WATCHER_STARTED", "Live P&L watcher started")
    state, session_warned = {}, False
    while not stop_flag.exists():
        cfg = store.load_config()
        try:
            has_open = poll_once(state)
            session_warned = False
            delay = cfg["poll_seconds"] if has_open and market_open() else 10
        except kite.SessionExpired as e:
            if not session_warned:
                _emit("SESSION_EXPIRED", str(e))
                session_warned = True
            _heartbeat_only()
            delay = 30
        except (kite.KiteError, paper.CoachError, TimeoutError) as e:
            print(f"poll error: {e}", file=sys.stderr, flush=True)
            _heartbeat_only()
            delay = 10
        time.sleep(delay)
    stop_flag.unlink(missing_ok=True)
    _emit("WATCHER_STOPPED", "Live P&L watcher stopped")


def _heartbeat_only():
    live = store.read_json("live.json", {})
    live["heartbeat"] = time.time()
    store.write_json("live.json", live)


def follow_events():
    log = store.path("events.log")
    log.touch(exist_ok=True)
    with open(log, encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(1)
                continue
            try:
                e = json.loads(line)
                print(f"[{e['ts'][11:19]}] {e['kind']}: {e['message']}", flush=True)
            except ValueError:
                pass


if __name__ == "__main__":
    if "--follow-events" in sys.argv:
        follow_events()
    else:
        run()
