---
description: View or set up your trading style and setup playbook
argument-hint: "[optional: describe your style or a setup]"
---
Show the trader's saved style and setups with `get_profile`: style as a short list, setups as a table
(name, timeframe, criteria).

If the profile is empty or they asked to set it up, interview them briefly, one or two questions at a
time: trader type, instruments, timeframes, typical holding time, session, strengths, the mistake they
repeat most, then their 2-5 setups with concrete criteria. Save with `update_profile` / `save_setup` in their
own words. If they described something in $ARGUMENTS, save that directly and confirm.
