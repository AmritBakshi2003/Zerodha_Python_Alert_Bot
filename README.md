# Kite Connect Trade-Alert Bot (alerts only)

Streams live data from Zerodha Kite Connect, evaluates a blended rule set on every
closed 1-minute candle, and pushes a **BUY/SELL alert with a one-line reason**.
**It never places orders** — only market-data endpoints are used.

## Signal logic
Five rules each vote bullish (+1), bearish (−1) or neutral (0):

| Rule | Bullish | Bearish |
|---|---|---|
| EMA cross | fast EMA crossed above slow within last N candles | crossed below |
| RSI | RSI within `[RSI_BUY_MIN, RSI_BUY_MAX]` | within `[RSI_SELL_MIN, RSI_SELL_MAX]` |
| MACD | MACD > signal and histogram rising | MACD < signal and histogram falling |
| ORB | close above opening-range high (first `ORB_MINUTES` from 09:15) | close below range low |
| Open interest | OI up + price up (long buildup) or OI down + price up (short covering) | OI up + price down (short buildup) or OI down + price down (long unwinding) |

An alert fires when **≥ `MIN_AGREE` rules agree and none disagree**. Equities have no OI,
so that rule is skipped automatically and `MIN_AGREE` is capped to the rules available.
`COOLDOWN_MINUTES` stops repeat alerts on the same side.

Example alert:
`🟢 BUY DEMO:FUT @ 100.02 [10:07] — EMA9 crossed above EMA21; RSI 72.1 in bullish zone; MACD above signal, histogram rising; OI +1.11% with price up (long buildup)`

## Setup
```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # fill KITE_API_KEY, KITE_API_SECRET, SYMBOLS, alert channel
python login.py             # run once each trading day (token expires daily)
python main.py              # run during market hours
```
Requires a Kite Connect app/subscription with historical-data access.

**Telegram:** create a bot with @BotFather → put token in `TELEGRAM_BOT_TOKEN`; send the bot a
message, then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`; set
`NOTIFY_CHANNELS=console,telegram`. **Slack:** create an Incoming Webhook → `SLACK_WEBHOOK_URL`,
`NOTIFY_CHANNELS=console,slack`. (Telegram is my recommendation — instant, free, works on mobile.)

## Demo (no account needed)
```bash
python demo.py
```
Replays a synthetic day through the same engine. See `demo_output.txt`.

## Files
| File | Purpose |
|---|---|
| `config.py` / `.env` | all keys and tunable parameters |
| `indicators.py` | EMA, RSI, MACD, opening range maths |
| `signals.py` | the five rules + blend logic |
| `engine.py` | candle history, cooldown, dispatch |
| `feed.py` | Kite history + websocket, candle builder, retries, reconnect back-fill |
| `notifier.py` | console / Telegram / Slack with retry |
| `main.py`, `login.py`, `demo.py` | live runner, daily token helper, paper demo |

## Robustness
- API calls retry with exponential backoff (rate limits/network); auth errors fail fast with a clear message.
- WebSocket auto-reconnects; after a reconnect, missed candles are back-filled silently.
- Exceptions in tick handling or rule evaluation are logged, never crash the feed.
- Notification failures are retried and never block the data path.

## Customising
Change periods/thresholds in `.env`. To add a rule, write `rule_xyz(df, p)` in `signals.py`
returning `(+1/-1/0, text)` or `None`, and append it to `RULES`.

## Notes
- Candles close when the next minute's first tick arrives, so illiquid symbols alert slightly late.
- OI comes from FULL-mode ticks and only exists for F&O instruments.
- Defaults are illustrative, not a validated strategy. Backtest before relying on them. Not financial advice.
