"""Check that this machine is ready to run the trading-coach plugin.

    python trading-coach/server/doctor.py

Verifies Python, the kiteconnect package (needed for live streaming), the data
folder, Kite configuration and network access. Secrets are never printed.
"""
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
problems = 0


def report(status, message, fix=None):
    global problems
    problems += status == FAIL
    print(f"{status} {message}")
    if fix and status != OK:
        print(f"       -> {fix}")


def main():
    v = sys.version_info
    report(OK if v >= (3, 9) else FAIL, f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}",
           "Install Python 3.9 or newer from https://www.python.org/downloads/ and tick 'Add to PATH'.")
    if v < (3, 9):
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from coach import analytics, behavior, kite, paper, store  # noqa: F401
        report(OK, "Plugin modules import")
    except Exception as e:  # noqa: BLE001
        report(FAIL, f"Plugin modules failed to import: {e}", "Re-install the plugin; files may be missing.")
        return

    try:
        import kiteconnect
        from kiteconnect.__version__ import __version__ as kc_version
        from kiteconnect import KiteTicker  # noqa: F401  (pulls in Twisted/autobahn for streaming)
        report(OK, f"kiteconnect {kc_version} installed (REST + WebSocket streaming)")
    except ImportError as e:
        report(FAIL, f"kiteconnect not importable for this Python ({e})",
               f'Run: "{sys.executable}" -m pip install "kiteconnect>=5.2,<6"')

    try:
        probe = store.path(".write-test")
        probe.write_text("ok")
        probe.unlink()
        report(OK, f"Data folder writable: {store.HOME}")
    except OSError as e:
        report(FAIL, f"Cannot write to {store.HOME}: {e}", "Set TRADING_COACH_HOME to a writable folder.")

    cfg = store.load_config()
    if cfg["price_source"] == "manual":
        report(OK, "price_source = manual (Kite not needed; you'll enter prices yourself)")
    else:
        report(OK if cfg["kite_api_key"] else WARN, "Kite API key " + ("set" if cfg["kite_api_key"] else "missing"),
               f"Add kite_api_key to {store.path('config.json')} (README section 3).")
        report(OK if cfg["kite_api_secret"] else WARN, "Kite API secret " + ("set" if cfg["kite_api_secret"] else "missing"),
               f"Add kite_api_secret to {store.path('config.json')} (README section 3).")
        s = kite.session_status()
        if not s["logged_in"]:
            report(WARN, "Not logged in to Kite today", "In Claude: /trading-coach:login")
        elif s["possibly_expired"]:
            report(WARN, f"Kite login is from {s['login_date']} (tokens reset daily)", "In Claude: /trading-coach:login")
        else:
            report(OK, f"Kite session from today (user {s['user_id']})")

        try:
            urllib.request.urlopen("https://api.kite.trade", timeout=8)
            report(OK, "api.kite.trade reachable")
        except urllib.error.HTTPError:
            report(OK, "api.kite.trade reachable")
        except Exception as e:  # noqa: BLE001
            report(FAIL, f"Cannot reach api.kite.trade: {e}", "Check internet / proxy / firewall.")

        port = int(cfg["kite_redirect_port"])
        with socket.socket() as sock:
            busy = sock.connect_ex(("127.0.0.1", port)) == 0
        report(WARN if busy else OK, f"Login redirect port {port} " + ("in use" if busy else "free"),
               "Change kite_redirect_port in config.json and the Redirect URL in your Kite app.")

    print()
    print("Ready (see any WARN lines above)." if not problems else f"{problems} problem(s) to fix before using the plugin.")


if __name__ == "__main__":
    main()
