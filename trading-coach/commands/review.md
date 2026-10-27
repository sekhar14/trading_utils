---
description: End-of-day or weekly discipline review of your paper trades
argument-hint: "[days, default 1]"
---
Call `review` with days = $ARGUMENTS (default 1) and `journal` for today.

Write a short review, trader-to-trader:
1. Numbers: trades, win rate, P&L. Then **P&L when rules were followed vs broken**. That is the headline.
2. Patterns: most frequent flags (revenge, rapid fire, size-up, no SL), overrides taken and how they ended,
   exits driven by fear vs plan, P&L by self-reported emotion.
3. One thing done well, one habit to fix tomorrow, framed as a concrete rule they could add
   (offer to add it with `add_checklist_rule` or `set_limit`).
No market opinions, no "you should have bought X".
