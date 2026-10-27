---
description: Visual analytics of your trading style, setups and discipline
argument-hint: "[days, default 30]"
---
Call `style_report` with days = $ARGUMENTS (default 30). If it was written, give the trader the report
path (and open it in a browser if you have one available). Then summarise in chat:
1. Headline numbers: trades, win rate, net P&L, per-trade expectancy, max drawdown.
2. The insights, best/worst setup first.
3. Stated style vs observed style. Point out any gap.
4. One concrete change to try next week, offered as a rule or playbook edit.
If there are no closed trades yet, say so and suggest a few paper trades first.
