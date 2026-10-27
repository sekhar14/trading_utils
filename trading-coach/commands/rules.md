---
description: View or change your trading rules (limits and pre-trade checklist)
argument-hint: "[optional change, e.g. 'max 2 trades a day' or 'add: only trade after first 15 min candle']"
---
Show the trader's current rules with `get_rules`: limits as a compact table, checklist as a numbered list.

If they asked for a change ($ARGUMENTS), map it to `set_limit` (numeric/boolean limits) or
`add_checklist_rule` / `remove_checklist_rule` (yes/no rules phrased so YES = followed), apply it,
and show the updated rules. If the request is ambiguous, ask once before changing anything.
If they have no checklist yet, offer to help them write 3-5 rules from their own strategy, in their words.
