"""Live runner. READ-ONLY: only market-data calls are made; no orders are ever placed."""
import logging
import sys
from kiteconnect import KiteConnect

from config import load_params, load_settings
from engine import Engine
from feed import KiteFeed, fetch_history, with_retry
from notifier import Notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def main():
    s, p = load_settings(), load_params()
    if not (s["api_key"] and s["access_token"] and s["symbols"]):
        sys.exit("Set KITE_API_KEY, KITE_ACCESS_TOKEN and SYMBOLS in .env (run login.py for the token).")

    kite = KiteConnect(api_key=s["api_key"])
    kite.set_access_token(s["access_token"])

    # Resolve "NSE:INFY" -> instrument_token via the LTP endpoint
    ltp = with_retry(kite.ltp, s["symbols"])
    token_map = {sym: ltp[sym]["instrument_token"] for sym in s["symbols"] if sym in ltp}
    missing = set(s["symbols"]) - set(token_map)
    if missing:
        log.warning("could not resolve: %s", ", ".join(missing))

    notifier = Notifier(s["channels"], s["telegram_token"], s["telegram_chat"], s["slack_webhook"])
    engine = Engine(p, notifier)
    for sym, tok in token_map.items():             # warm indicators from recent history
        engine.merge(sym, fetch_history(kite, tok, days=3))
        log.info("seeded %s with %d candles", sym, len(engine.frames[sym]))

    notifier.send(f"✅ Alert bot started — watching {', '.join(token_map)} (alerts only, no orders)")
    KiteFeed(kite, s["api_key"], s["access_token"], token_map, engine).run()


if __name__ == "__main__":
    main()
