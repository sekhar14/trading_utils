"""Live P&L watcher for paper positions, streaming ticks over Kite's WebSocket (KiteTicker).

    python watcher.py                  run the watcher (started detached by the MCP server)
    python watcher.py --follow-events  print new coach events as they happen (for Claude Code's Monitor)

Prices are pushed by Kite tick by tick; nothing is polled. Each tick for an open
paper position is checked immediately against its stop-loss and target, the
day's P&L is checked against the loss limit, and the results go to live.json
(current snapshot) and events.log (one JSON line per event).

The watcher exits when no positions are open or the Kite session expires;
the MCP server starts it again on the next entry or login.
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coach import behavior, kite, paper, store  # noqa: E402

HEARTBEAT_STALE_SECONDS = 20
IDLE_EXIT_SECONDS = 120          # no open positions for this long -> exit
BOOK_CHECK_SECONDS = 1.0         # how often to notice new/closed positions on disk


def is_running():
    live = store.read_json("live.json", {})
    return bool(live.get("heartbeat")) and time.time() - live["heartbeat"] < HEARTBEAT_STALE_SECONDS


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


def _in_reactor(fn, *args):
    """KiteTicker runs on Twisted's reactor thread; hand calls to it rather than sending from ours."""
    try:
        from twisted.internet import reactor
        if reactor.running:
            return reactor.callFromThread(fn, *args)
    except ImportError:
        pass
    return fn(*args)


class Watcher:
    def __init__(self, make_ticker=kite.ticker):
        self.make_ticker = make_ticker
        self.prices = {}              # instrument_token -> last_price (written by the ticker thread)
        self.tick_event = threading.Event()
        self.subscribed = set()
        self.connected = False
        self.fatal = None             # reason to stop (session expired, gave up reconnecting)
        self.fired = {}               # date -> loss-warning levels already announced
        self.book_mtime = None
        self.open_trades = []
        self.last_snapshot = 0.0

    # -------------------------------------------------- ticker callbacks (Twisted thread)

    def on_ticks(self, ws, ticks):
        for t in ticks:
            self.prices[t["instrument_token"]] = t["last_price"]
        self.tick_event.set()

    def on_connect(self, ws, response):
        self.connected = True
        self.subscribed = set()       # resubscribe after every (re)connect
        self.tick_event.set()

    def on_close(self, ws, code, reason):
        self.connected = False

    def on_error(self, ws, code, reason):
        if "403" in str(reason) or "403" in str(code):
            self.fatal = "SESSION_EXPIRED"
        self.tick_event.set()

    def on_reconnect(self, ws, attempts):
        print(f"reconnecting to Kite ticker (attempt {attempts})", file=sys.stderr, flush=True)

    def on_noreconnect(self, ws):
        self.fatal = "DISCONNECTED"
        self.tick_event.set()

    # -------------------------------------------------- main thread

    def _reload_book(self):
        """Pick up entries/exits made by the MCP server (another process)."""
        try:
            mtime = store.path("trades.json").stat().st_mtime
        except FileNotFoundError:
            mtime = 0
        if mtime == self.book_mtime:
            return False
        self.book_mtime = mtime
        book = store.load_book()
        self.open_trades = [t for t in book["trades"] if t["status"] == "open"]
        missing = [t["symbol"] for t in self.open_trades if not t.get("instrument_token")]
        if missing:  # older trades: look the token up once over REST
            tokens = {s: q["instrument_token"] for s, q in kite.quotes(missing).items()}
            for t in self.open_trades:
                t["instrument_token"] = t.get("instrument_token") or tokens.get(t["symbol"])
        return True

    def _sync_subscriptions(self, ws):
        if not self.connected:
            return
        wanted = {int(t["instrument_token"]) for t in self.open_trades if t.get("instrument_token")}
        add, drop = wanted - self.subscribed, self.subscribed - wanted
        if add:
            _in_reactor(ws.subscribe, list(add))
            _in_reactor(ws.set_mode, ws.MODE_LTP, list(add))
        if drop:
            _in_reactor(ws.unsubscribe, list(drop))
        self.subscribed = wanted

    def _evaluate(self):
        """Check SL/target and loss limits against the latest ticks."""
        by_token = {int(t["instrument_token"]): t for t in self.open_trades if t.get("instrument_token")}
        hits = []
        for token, trade in by_token.items():
            ltp = self.prices.get(token)
            if ltp is None:
                continue
            kind, level = _hit(trade, ltp)
            if kind:
                long = trade["side"] == "BUY"
                # Fill at the trigger level, or at the tick if price jumped through it.
                fill = min(level, ltp) if (kind == "stop_loss") == long else max(level, ltp)
                hits.append((trade["id"], kind, fill))
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
            self.book_mtime = None
            self._reload_book()

        book = store.load_book()
        prices_by_symbol = {t["symbol"]: self.prices[int(t["instrument_token"])]
                            for t in self.open_trades
                            if t.get("instrument_token") and int(t["instrument_token"]) in self.prices}
        st = behavior.day_stats(book)
        positions = paper.positions_with_pnl(book, prices_by_symbol)
        unrealized = sum(p["unrealized_pnl"] or 0 for p in positions)
        day_pnl = st["realized_pnl"] + unrealized

        rules = store.load_rules()
        max_loss = abs(rules["limits"]["max_daily_loss"])
        fired = self.fired.setdefault(st["date"], set())
        for pct in (50, 80, 100):
            if day_pnl <= -max_loss * pct / 100 and pct not in fired:
                fired.add(pct)
                if pct < 100:
                    _emit("LOSS_WARNING", f"Day P&L {day_pnl:,.0f} is {pct}% of your max daily loss ({max_loss:,}).",
                          day_pnl=day_pnl)
                else:
                    _emit("DAILY_LOSS_LIMIT", f"Day P&L {day_pnl:,.0f} hit max daily loss ({max_loss:,}). Stop trading for today.",
                          day_pnl=day_pnl)
                    if rules["limits"].get("auto_square_off_on_daily_loss") and self.open_trades:
                        closed = self._square_off(prices_by_symbol)
                        _emit("SQUARED_OFF", f"Closed {closed} position(s) at the last tick.")
        return positions, st["realized_pnl"], unrealized, day_pnl

    def _square_off(self, prices_by_symbol):
        with store.locked():
            book = store.load_book()
            n = 0
            for t in book["trades"]:
                if t["status"] == "open" and t["symbol"] in prices_by_symbol:
                    paper.close_trade(book, t, prices_by_symbol[t["symbol"]], "daily_loss_limit")
                    n += 1
            store.save_book(book)
        self.book_mtime = None
        self._reload_book()
        return n

    def _snapshot(self, positions, realized, unrealized, day_pnl):
        store.write_json("live.json", {
            "heartbeat": time.time(),
            "updated_at": store.now().isoformat(timespec="seconds"),
            "source": "kite_websocket",
            "connected": self.connected,
            "positions": positions,
            "realized_pnl": realized,
            "unrealized_pnl": round(unrealized, 2),
            "day_pnl": round(day_pnl, 2),
        })
        self.last_snapshot = time.time()

    def run(self):
        stop_flag = store.path("watcher.stop")
        stop_flag.unlink(missing_ok=True)
        self._reload_book()
        if not self.open_trades:
            print("no open paper positions; nothing to watch", flush=True)
            return
        try:
            ws = self.make_ticker()
        except kite.KiteError as e:
            _emit("SESSION_EXPIRED" if isinstance(e, kite.SessionExpired) else "WATCHER_ERROR", str(e))
            return
        for name in ("on_ticks", "on_connect", "on_close", "on_error", "on_reconnect", "on_noreconnect"):
            setattr(ws, name, getattr(self, name))
        ws.connect(threaded=True)
        _emit("WATCHER_STARTED", f"Streaming live ticks for {len(self.open_trades)} open position(s)")

        idle_since = None
        positions, realized, unrealized, day_pnl = [], 0.0, 0.0, 0.0
        try:
            while not stop_flag.exists() and not self.fatal:
                got_tick = self.tick_event.wait(timeout=BOOK_CHECK_SECONDS)
                self.tick_event.clear()
                self._reload_book()
                if got_tick or time.time() - self.last_snapshot > 5:
                    positions, realized, unrealized, day_pnl = self._evaluate()
                    self._snapshot(positions, realized, unrealized, day_pnl)
                self._sync_subscriptions(ws)
                if self.open_trades:
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > IDLE_EXIT_SECONDS:
                    break
        finally:
            if self.fatal == "SESSION_EXPIRED":
                _emit("SESSION_EXPIRED", "Kite rejected the ticker session (tokens reset daily). Log in again; "
                                         "the watcher restarts after login.")
            elif self.fatal == "DISCONNECTED":
                _emit("WATCHER_ERROR", "Lost the Kite ticker connection and could not reconnect.")
            stop_flag.unlink(missing_ok=True)
            live = store.read_json("live.json", {})
            live["heartbeat"] = 0
            store.write_json("live.json", live)
            _emit("WATCHER_STOPPED", "Live P&L watcher stopped")
            try:
                ws.close()
                ws.stop()
            except Exception:  # noqa: BLE001 - reactor may already be down
                pass


def follow_events():
    log = store.path("events.log")
    log.touch(exist_ok=True)
    with open(log, encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                e = json.loads(line)
                print(f"[{e['ts'][11:19]}] {e['kind']}: {e['message']}", flush=True)
            except ValueError:
                pass


if __name__ == "__main__":
    if "--follow-events" in sys.argv:
        follow_events()
    elif is_running():
        print("watcher already running", flush=True)
    else:
        Watcher().run()
