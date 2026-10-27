---
description: Open paper positions, live P&L and today's discipline stats
---
Call `coach_status`. Report in a few lines: each open position (symbol, side, qty, entry, LTP, P&L, SL,
target), day P&L (realized + unrealized), trades taken vs limit, consecutive losses, cooldown if any,
watcher running or not. Flag anything near a limit. If positions are open and the watcher is not
running, start it with `watcher_start` (and in Claude Code, Monitor its `follow_command`).
