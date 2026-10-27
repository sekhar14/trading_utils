# trading_utils

Claude plugins for traders, published as a Claude Code plugin marketplace.

| Plugin | What it does |
|---|---|
| [trading-coach](trading-coach/) | Discipline coach for discretionary F&O traders. Checks every trade against your own rules, flags overtrading and emotional decisions, and paper-trades on live Zerodha Kite prices with a live P&L watcher. |

## Quick start

```
/plugin marketplace add sekhar14/trading_utils
/plugin install trading-coach@trading-utils
```

Requirements: Python 3.9+ on `PATH` (no packages to install) and, for live prices, a Zerodha Kite Connect app.
Check your machine with `python trading-coach/server/doctor.py`.

Then follow [trading-coach/README.md](trading-coach/README.md) for Zerodha setup and daily use.

![Coach catching a revenge trade](trading-coach/docs/images/chat-revenge-trade.png)
