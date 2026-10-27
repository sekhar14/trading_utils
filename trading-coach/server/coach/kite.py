"""Read-only Zerodha Kite Connect client (stdlib only).

Only market-data and session endpoints are used. This module deliberately has
no order-placement code: the coach is paper trading only.
"""
import csv
import hashlib
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import store

API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"

# underlying -> (options exchange, spot quote symbol)
UNDERLYINGS = {
    "NIFTY": ("NFO", "NSE:NIFTY 50"),
    "BANKNIFTY": ("NFO", "NSE:NIFTY BANK"),
    "FINNIFTY": ("NFO", "NSE:NIFTY FIN SERVICE"),
    "MIDCPNIFTY": ("NFO", "NSE:NIFTY MID SELECT"),
    "SENSEX": ("BFO", "BSE:SENSEX"),
    "BANKEX": ("BFO", "BSE:BANKEX"),
}


class KiteError(Exception):
    pass


class SessionExpired(KiteError):
    pass


def _session():
    return store.read_json("kite_session.json", {})


def _request(method, path, params=None, form=None, auth=True):
    cfg = store.load_config()
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"X-Kite-Version": "3"}
    if auth:
        token = _session().get("access_token")
        if not cfg["kite_api_key"] or not token:
            raise SessionExpired("Not logged in to Kite. Run the kite_login tool (or /trading-coach:login).")
        headers["Authorization"] = f"token {cfg['kite_api_key']}:{token}"
    body = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)
        except ValueError:
            err = {"message": raw[:300]}
        if err.get("error_type") == "TokenException" or e.code == 403:
            raise SessionExpired("Kite session expired (tokens reset daily ~6 AM). Log in again.") from None
        raise KiteError(f"Kite API {e.code}: {err.get('message')}") from None
    except urllib.error.URLError as e:
        raise KiteError(f"Cannot reach Kite: {e.reason}") from None


def _json(method, path, **kw):
    payload = json.loads(_request(method, path, **kw))
    return payload.get("data")


# ---------------------------------------------------------------- session

def login_url():
    cfg = store.load_config()
    if not cfg["kite_api_key"]:
        raise KiteError("kite_api_key is not set. Add it to ~/.trading-coach/config.json or KITE_API_KEY.")
    return LOGIN_URL.format(api_key=cfg["kite_api_key"])


def exchange_request_token(request_token):
    cfg = store.load_config()
    if not cfg["kite_api_secret"]:
        raise KiteError("kite_api_secret is not set. Add it to ~/.trading-coach/config.json or KITE_API_SECRET.")
    checksum = hashlib.sha256((cfg["kite_api_key"] + request_token + cfg["kite_api_secret"]).encode()).hexdigest()
    data = _json("POST", "/session/token", auth=False,
                 form={"api_key": cfg["kite_api_key"], "request_token": request_token, "checksum": checksum})
    store.write_json("kite_session.json", {
        "access_token": data["access_token"],
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "login_date": store.today_str(),
    })
    return {"user_id": data.get("user_id"), "user_name": data.get("user_name")}


def session_status():
    s = _session()
    if not s.get("access_token"):
        return {"logged_in": False}
    return {"logged_in": True, "user_id": s.get("user_id"), "login_date": s.get("login_date"),
            "possibly_expired": s.get("login_date") != store.today_str()}


def parse_request_token(text):
    """Accept a bare request_token or the full redirect URL the browser landed on."""
    text = text.strip()
    if "request_token=" in text:
        qs = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        return urllib.parse.parse_qs(qs)["request_token"][0]
    return text


def start_callback_server(timeout=300):
    """Listen on 127.0.0.1 for Kite's redirect and finish login automatically.

    Kite app's Redirect URL must be http://127.0.0.1:<kite_redirect_port>/kite/callback
    """
    port = int(store.load_config()["kite_redirect_port"])
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = qs.get("request_token", [None])[0]
            if token:
                try:
                    result.update(exchange_request_token(token))
                    msg = "Kite login complete. You can close this tab and go back to Claude."
                except KiteError as e:
                    result["error"] = str(e)
                    msg = f"Login failed: {e}"
            else:
                msg = "No request_token in redirect."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())
            if token:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    threading.Timer(timeout, server.shutdown).start()
    return server, thread, result


# ---------------------------------------------------------------- market data

def ltp(symbols):
    """symbols like 'NFO:NIFTY25O2824500CE' -> {symbol: last_price}"""
    if not symbols:
        return {}
    data = _json("GET", "/quote/ltp", params={"i": list(symbols)})
    return {k: v["last_price"] for k, v in (data or {}).items()}


def instruments(exchange):
    """Option contracts for supported underlyings, cached per exchange per day."""
    cache = f"instruments_{exchange}_{store.today_str()}.json"
    if store.path(cache).exists():
        return store.read_json(cache, [])
    raw = _request("GET", f"/instruments/{exchange}").decode("utf-8")
    rows = []
    for r in csv.DictReader(io.StringIO(raw)):
        if r["name"] in UNDERLYINGS and r["instrument_type"] in ("CE", "PE"):
            rows.append({
                "tradingsymbol": r["tradingsymbol"], "name": r["name"], "exchange": r["exchange"],
                "expiry": r["expiry"], "strike": float(r["strike"]), "option_type": r["instrument_type"],
                "lot_size": int(r["lot_size"]), "tick_size": float(r["tick_size"]),
            })
    for old in store.HOME.glob(f"instruments_{exchange}_*.json"):
        old.unlink(missing_ok=True)
    store.write_json(cache, rows)
    return rows


def resolve_option(underlying, option_type, strike="ATM", expiry="nearest"):
    """Turn 'NIFTY CE ATM' into a concrete contract with live prices.

    strike: "ATM", "ATM+2" / "ATM-1" (steps up/down the strike ladder), or a number.
    expiry: "nearest", "next", or YYYY-MM-DD.
    """
    underlying = underlying.upper().replace(" ", "")
    option_type = option_type.upper()
    if underlying not in UNDERLYINGS:
        raise KiteError(f"Unsupported underlying {underlying}. Supported: {', '.join(UNDERLYINGS)}")
    exchange, spot_symbol = UNDERLYINGS[underlying]
    contracts = [c for c in instruments(exchange) if c["name"] == underlying and c["option_type"] == option_type]
    expiries = sorted({c["expiry"] for c in contracts if c["expiry"] >= date.today().isoformat()})
    if not expiries:
        raise KiteError(f"No live {underlying} {option_type} contracts found")
    if expiry == "nearest":
        exp = expiries[0]
    elif expiry == "next":
        exp = expiries[min(1, len(expiries) - 1)]
    elif expiry in expiries:
        exp = expiry
    else:
        raise KiteError(f"Expiry {expiry} not found. Available: {', '.join(expiries[:6])}")
    ladder = sorted((c for c in contracts if c["expiry"] == exp), key=lambda c: c["strike"])

    spot = ltp([spot_symbol]).get(spot_symbol)
    s = str(strike).upper().replace(" ", "")
    if s.startswith("ATM"):
        atm_idx = min(range(len(ladder)), key=lambda i: abs(ladder[i]["strike"] - spot))
        offset = int(s[3:] or 0)
        idx = max(0, min(len(ladder) - 1, atm_idx + offset))
    else:
        target = float(s)
        idx = min(range(len(ladder)), key=lambda i: abs(ladder[i]["strike"] - target))
        if ladder[idx]["strike"] != target:
            raise KiteError(f"Strike {target:g} not listed for {exp}; nearest is {ladder[idx]['strike']:g}")
    contract = dict(ladder[idx])
    symbol = f"{contract['exchange']}:{contract['tradingsymbol']}"
    contract["symbol"] = symbol
    contract["ltp"] = ltp([symbol]).get(symbol)
    contract["spot"] = spot
    contract["spot_symbol"] = spot_symbol
    contract["underlying"] = underlying
    return contract
