"""Paper-trading engine: fills at live Kite LTP (or a manual price), never at a broker."""
from . import behavior, kite, store


class CoachError(Exception):
    pass


def _tick_round(price, tick=0.05):
    return round(round(price / tick) * tick, 2)


def live_prices(symbols):
    if store.load_config()["price_source"] == "manual":
        raise CoachError("price_source is 'manual': pass the current price explicitly.")
    return kite.ltp(symbols)


def pnl_of(trade, price):
    sign = 1 if trade["side"] == "BUY" else -1
    return round((price - trade["entry_price"]) * trade["qty"] * sign, 2)


def positions_with_pnl(book, prices=None):
    open_trades = [t for t in book["trades"] if t["status"] == "open"]
    if prices is None:
        try:
            prices = live_prices([t["symbol"] for t in open_trades])
        except (CoachError, kite.KiteError):
            prices = {}
    out = []
    for t in open_trades:
        ltp = prices.get(t["symbol"])
        out.append({"id": t["id"], "symbol": t["symbol"], "side": t["side"], "lots": t["lots"], "qty": t["qty"],
                    "entry_price": t["entry_price"], "ltp": ltp, "stop_loss": t["stop_loss"], "target": t["target"],
                    "unrealized_pnl": pnl_of(t, ltp) if ltp is not None else None, "opened_at": t["opened_at"]})
    return out


def check(proposal):
    book = store.load_book()
    unrealized = sum(p["unrealized_pnl"] or 0 for p in positions_with_pnl(book))
    return behavior.pre_trade_check(book, store.load_rules(), proposal, unrealized, store.load_profile())


def enter(contract, side, lots, stop_loss, target, reason, checklist_answers, emotion=None,
          override_reason=None, price=None, setup=None, timeframe=None):
    """contract: output of kite.resolve_option (or the same keys for manual mode)."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise CoachError("side must be BUY or SELL")
    cfg = store.load_config()
    rules = store.load_rules()
    tick = contract.get("tick_size") or 0.05

    if price is None:
        ltp = live_prices([contract["symbol"]]).get(contract["symbol"])
        if ltp is None:
            raise CoachError(f"No live price for {contract['symbol']}")
        slip = cfg["slippage_ticks"] * tick
        price = _tick_round(ltp + slip if side == "BUY" else ltp - slip, tick)
    else:
        ltp = price

    proposal = {"underlying": contract["underlying"], "option_type": contract["option_type"],
                "tradingsymbol": contract["tradingsymbol"], "side": side, "lots": lots,
                "lot_size": contract["lot_size"], "price": price, "stop_loss": stop_loss,
                "target": target, "reason": reason, "setup": setup}
    result = check(proposal)
    flags = result["flags"]

    answers = checklist_answers or {}
    missing = [c["id"] for c in rules["checklist"] if c["id"] not in answers]
    if missing:
        raise CoachError(f"Checklist not answered: {', '.join(missing)}. Ask the trader each item first.")
    for c in rules["checklist"]:
        if not answers[c["id"]]:
            flags.append({"code": "CHECKLIST_NO", "severity": "WARN", "message": f"Answered NO to: {c['text']}"})

    if result["blocked"] and not (override_reason or "").strip():
        return {"entered": False, "blocked": True, "flags": flags,
                "message": "Hard rule broken. Entry refused unless the trader gives an explicit override reason."}

    with store.locked():
        book = store.load_book()
        day_count = sum(1 for t in book["trades"] if t["opened_at"][:10] == store.today_str())
        trade = {
            "id": f"P{store.now():%Y%m%d}-{day_count + 1:03d}",
            "status": "open",
            "opened_at": store.now().isoformat(timespec="seconds"),
            "underlying": contract["underlying"], "symbol": contract["symbol"],
            "tradingsymbol": contract["tradingsymbol"], "option_type": contract["option_type"],
            "strike": contract.get("strike"), "expiry": contract.get("expiry"),
            "spot_at_entry": contract.get("spot"), "side": side, "lots": int(lots),
            "lot_size": int(contract["lot_size"]), "tick_size": tick, "qty": int(lots) * int(contract["lot_size"]),
            "ltp_at_entry": ltp, "entry_price": price,
            "stop_loss": float(stop_loss) if stop_loss not in (None, "") else None,
            "target": float(target) if target not in (None, "") else None,
            "setup": setup, "timeframe": timeframe,
            "reason": reason, "emotion": emotion, "checklist_answers": answers,
            "flags": flags, "override_reason": override_reason,
            "rules_followed": not any(f["severity"] == "BLOCK" or f["code"] == "CHECKLIST_NO" for f in flags),
        }
        book["trades"].append(trade)
        store.save_book(book)
    store.append_event("ENTRY", f"{trade['id']} {side} {trade['lots']} lot {trade['tradingsymbol']} @ {price}",
                       trade_id=trade["id"])
    return {"entered": True, "trade": trade}


def close_trade(book, trade, price, exit_reason, note=None):
    """Mutates book in place; caller holds the lock and saves."""
    trade["status"] = "closed"
    trade["closed_at"] = store.now().isoformat(timespec="seconds")
    trade["exit_price"] = price
    trade["exit_reason"] = exit_reason
    trade["exit_note"] = note
    trade["pnl"] = pnl_of(trade, price)
    return trade


def exit_trades(trade_id="all", reason="manual", note=None, price=None):
    with store.locked():
        book = store.load_book()
        targets = [t for t in book["trades"] if t["status"] == "open" and trade_id in ("all", t["id"])]
        if not targets:
            raise CoachError(f"No open paper position matching {trade_id}")
        if price is None:
            prices = live_prices([t["symbol"] for t in targets])
        closed = []
        for t in targets:
            tick = t.get("tick_size", 0.05)
            if price is not None:
                fill = float(price)
            else:
                ltp = prices.get(t["symbol"])
                if ltp is None:
                    raise CoachError(f"No live price for {t['symbol']}")
                slip = store.load_config()["slippage_ticks"] * tick
                fill = _tick_round(ltp - slip if t["side"] == "BUY" else ltp + slip, tick)
            closed.append(close_trade(book, t, fill, reason, note))
        store.save_book(book)
    for t in closed:
        store.append_event("EXIT", f"{t['id']} closed @ {t['exit_price']} ({reason}) P&L {t['pnl']:,.0f}",
                           trade_id=t["id"], pnl=t["pnl"])
    st = behavior.day_stats(store.load_book())
    return {"closed": closed, "day_realized_pnl": st["realized_pnl"], "consecutive_losses": st["consecutive_losses"]}
