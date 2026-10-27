---
description: Start live P&L monitoring so the coach reacts to SL/target hits and loss limits
---
Call `watcher_start`. Then, in Claude Code, start the Monitor tool (persistent) on the returned
`follow_command` so every coach event (SL_HIT, TARGET_HIT, LOSS_WARNING, DAILY_LOSS_LIMIT,
SESSION_EXPIRED) arrives in this conversation, and respond to each as the trade-coach skill describes.
In Claude Desktop (no Monitor), tell the trader the watcher still auto-exits at SL/target in the
background, and that `/trading-coach:status` shows the latest P&L and events.
