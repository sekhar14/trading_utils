"""Generate a sample trader's history so you can preview the analytics report.

    python trading-coach/server/demo.py            # writes to ~/.trading-coach-demo and builds a report

Uses its own folder, so your real ~/.trading-coach data is never touched.
The trades are synthetic (seeded random), not market data.
"""
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEMO_HOME = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".trading-coach-demo"
os.environ["TRADING_COACH_HOME"] = str(DEMO_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coach import analytics, store  # noqa: E402

SETUPS = {  # name: (win probability, avg win pts, avg loss pts, typical entry minutes after 09:15)
    "ORB 15m": (0.58, 28, 14, [15, 25, 35]),
    "VWAP reclaim": (0.52, 22, 13, [60, 90, 150, 200]),
    "PDH breakout": (0.42, 30, 18, [20, 45, 120]),
    "Gut feel": (0.30, 14, 20, [10, 140, 250, 300]),
}


def main():
    DEMO_HOME.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    store.save_profile({
        "style": {**store.DEFAULT_PROFILE["style"],
                  "trader_type": "Intraday option buyer", "instruments": ["NIFTY", "BANKNIFTY"],
                  "timeframes": ["5m", "15m"], "typical_holding": "20-40 min", "session": "09:30-11:30 mostly",
                  "weaknesses": "Revenge trades after the first loss; trades without a setup when bored"},
        "setups": [
            {"name": "ORB 15m", "description": "Break of the 15-min opening range", "timeframe": "15m",
             "criteria": ["15m range defined", "Close outside range", "Volume above 20-bar average"]},
            {"name": "VWAP reclaim", "description": "Price reclaims VWAP after a pullback", "timeframe": "5m",
             "criteria": ["Two closes above VWAP", "Higher low formed", "Index trend aligned"]},
            {"name": "PDH breakout", "description": "Previous day high break and hold", "timeframe": "5m",
             "criteria": ["Clean PDH level", "Retest holds", "No major event in next 30 min"]},
        ],
    })

    trades = []
    day = store.now().replace(hour=9, minute=15, second=0, microsecond=0) - timedelta(days=27)
    while day.date() <= store.now().date():
        if day.weekday() < 5:
            n = rng.choice([1, 2, 2, 3, 3, 4])
            last_loss_at = None
            for i in range(n):
                setup = rng.choices(list(SETUPS), weights=[4, 3, 2, 2])[0]
                p_win, w_pts, l_pts, starts = SETUPS[setup]
                start = day + timedelta(minutes=rng.choice(starts) + rng.randint(0, 20))
                revenge = last_loss_at is not None and rng.random() < 0.5
                if revenge:
                    start = last_loss_at + timedelta(minutes=rng.randint(3, 12))
                    setup = rng.choice([setup, "Gut feel"])
                    p_win -= 0.15
                if trades and start <= datetime.fromisoformat(trades[-1]["closed_at"]):
                    start = datetime.fromisoformat(trades[-1]["closed_at"]) + timedelta(minutes=5)
                if start.hour >= 15:
                    break
                win = rng.random() < p_win
                hold = rng.randint(12, 45) if win else rng.randint(3, 25)
                lots = 2 if revenge and rng.random() < 0.6 else 1
                entry = round(rng.uniform(90, 160), 1)
                pts = rng.gauss(w_pts, 6) if win else -abs(rng.gauss(l_pts, 4))
                exit_p = round(entry + pts, 1)
                underlying = rng.choices(["NIFTY", "BANKNIFTY"], weights=[3, 1])[0]
                lot_size = 65 if underlying == "NIFTY" else 30
                emotion = ("revenge" if revenge else
                           rng.choices(["calm", "confident", "fomo", "bored"], weights=[6, 3, 2, 1])[0])
                flags = []
                if revenge:
                    flags += [{"code": "COOLDOWN", "severity": "WARN", "message": ""}]
                    if lots == 2:
                        flags += [{"code": "SIZE_UP_AFTER_LOSS", "severity": "WARN", "message": ""}]
                if setup == "Gut feel":
                    flags += [{"code": "OFF_PLAYBOOK", "severity": "WARN", "message": ""}]
                broke = revenge or setup == "Gut feel" or emotion == "fomo"
                closed = start + timedelta(minutes=hold)
                trades.append({
                    "id": f"P{start:%Y%m%d}-{i + 1:03d}", "status": "closed",
                    "opened_at": start.isoformat(timespec="seconds"), "closed_at": closed.isoformat(timespec="seconds"),
                    "underlying": underlying, "symbol": f"NFO:{underlying}DEMO", "tradingsymbol": f"{underlying} DEMO",
                    "option_type": rng.choice(["CE", "PE"]), "side": "BUY", "lots": lots, "lot_size": lot_size,
                    "qty": lots * lot_size, "entry_price": entry, "exit_price": exit_p,
                    "stop_loss": round(entry - l_pts, 1), "target": round(entry + w_pts * 1.2, 1),
                    "setup": setup, "timeframe": "5m", "reason": "demo", "emotion": emotion,
                    "flags": flags, "rules_followed": not broke,
                    "exit_reason": ("target" if win and rng.random() < 0.5 else "plan_exit") if win
                    else rng.choices(["stop_loss", "fear"], weights=[3, 1])[0],
                    "pnl": round((exit_p - entry) * lots * lot_size, 2),
                })
                last_loss_at = closed if trades[-1]["pnl"] < 0 else None
        day += timedelta(days=1)

    store.save_book({"trades": trades})
    store.save_rules(store.DEFAULT_RULES)
    out = analytics.write_report(store.load_book(), store.load_profile(), days=30)
    print(f"Demo data: {len(trades)} trades in {DEMO_HOME}")
    print(f"Report:    {out.get('path')}")


if __name__ == "__main__":
    main()
