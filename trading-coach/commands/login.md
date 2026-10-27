---
description: Log in to Zerodha Kite for live market data (read-only, needed once per day)
---
Call `kite_login` and give the trader the `login_url` to open and log in themselves on Zerodha's page.
Never ask for their password or TOTP. When they say they're done, confirm with `coach_status`.
If it did not complete automatically, ask them to paste the URL their browser landed on and call
`kite_complete_login` with it.

If `kite_login` errors because the API key/secret is missing, explain the one-time setup from the
plugin README (create a Kite Connect app, set its redirect URL to the `redirect_url_expected` value,
and put the key and secret in `~/.trading-coach/config.json` themselves).
