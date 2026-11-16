# Kite Alerts — Project Roadmap & Technical Architecture Context

This document captures the current architectural state, chronological build history (Phases C1 through C4), and prioritized future development scopes for the **Zerodha Kite Connect Trade-Alert Bot**. It serves as the primary technical context for ongoing and future engineering sessions.

---

## 1. Architectural Philosophy & Core Invariants

The project is built on strict design principles that must be maintained across all future phases:

1. **Alerts Only (Zero Execution Risk)**:
   - The bot evaluates market data and generates diagnostic alerts (BUY/SELL) with explicit human-readable reasons.
   - It **never** places, modifies, cancels, or monitors orders, trades, positions, or holdings.
   - Mechanically enforced in CI: `tests/test_alerts_only.py` scans all Python, JavaScript, and HTML files for Kite order-side method names (`place_order`, `modify_order`, `cancel_order`, etc.), failing the build if any are introduced.
2. **Bring Your Own Key (BYOK)**:
   - Designed for personal single-user deployment.
   - No shared API keys, hosted proxy backends, or third-party credential brokers.
   - Authentication tokens stay local and are redacted in all logs, exceptions, and API payloads.
3. **Local Loopback Security**:
   - The web UI binds strictly to loopback (`127.0.0.1`), enforces strict `Host` header checks, serves restrictive Content Security Policies (CSP: `default-src 'self'`), requires same-origin headers on all mutations, and uses CSRF protection.
4. **Decoupled Alert & Chart Paths**:
   - The Chart workstation live feed (`LiveFeedService`) streams market data for visualization only. It never touches the strategy evaluator, the alert journal, or the `NotifierHub`.
   - The alert engine (`kite-alerts run`) remains the single source of truth for alerts.
5. **Quality & Test Gates**:
   - Strict 85.0% minimum branch/statement test coverage enforced via `pyproject.toml` (`fail-under = 85`).
   - Linted with `ruff check`, formatted with `ruff format`, type-checked with `mypy` in strict mode.
   - Zero external network dependencies in automated tests (all mocked via `FakeKite`, `FakeTicker`, and public fixture stubs).

---

## 2. Chronological Build History & Implemented State

### Phase C1: Headless Engine & Offline Practice Player
- **Strategy & Evaluator**: 5 modular voting rules:
  - `EMA cross`: Fast/slow exponential moving average crossover with lookback buffer.
  - `RSI zone`: Relative strength index bullish/bearish thresholds with momentum confirmation.
  - `MACD cross`: Line vs signal crossover combined with rising/falling histogram momentum.
  - `ORB`: Opening Range Breakout evaluated against session opening window (09:15 to 09:15+N minutes).
  - `Open Interest (OI)`: Analyzes price vs open interest deltas to identify long buildup, short covering, short buildup, and long unwinding.
- **Resilient Delivery (`NotifierHub`)**:
  - Non-blocking in-memory queue delivering to Console, Telegram, Slack, and SMTP.
  - Bounded queue (`NOTIFY_QUEUE_MAX`, default 5 000): on overflow the oldest system message is evicted first, then the oldest alert, so the newest alerts survive; every drop is counted (`notifier_dropped_total`) and logged.
  - Per-channel `TokenBucket` rate-limit enforcement and exponential backoff retry with jitter.
  - Duplicate message suppression via TTL dedup keys.
  - Dead Letter Queue (`logs/dead-letter.jsonl`) preserving delivery failures with diagnostic context.
- **Local Dashboard & Practice Demo**:
  - FastAPI server serving lightweight single-page application (vanilla JS, modern CSS).
  - Offline synthetic demo player reproducing the golden demo output (`kite-alerts demo`).
  - Read-only SQLite alert history explorer (`/api/alerts`).

### Phase C2: Settings Management, Guided Connect & Diagnostic Notifier Setup
- **Atomic Settings Engine**:
  - Dynamic UI schema derived directly from Pydantic settings models.
  - Atomic write mechanism preserving existing `.env` formatting and comments, with automatic rolling backups (`.env.bak-<timestamp>`).
- **Guided Kite Connect Wizard**:
  - Interactive 4-step OAuth loopback login with callback validation (`/callback`).
  - Account profile probe and real-time market-data entitlement detection.
- **Notification Assistants**:
  - Telegram chat ID auto-discovery via `getUpdates` parsing (`/api/notifications/telegram/chat-id`).
  - Interactive single-click diagnostic test alert system (`/api/test-alert`) with per-channel diagnostics.

### Phase C3: Chart Workstation & Multi-Source Architecture
- **TradingView Lightweight-Charts Workstation**:
  - High-performance canvas chart supporting Candles, Hollow Candles, Heikin-Ashi, OHLC Bars, Line, Area, and Baseline styles.
  - Multiple timeframes: 1m native, and resampled 3m, 5m, 15m.
  - Server-computed technical overlays matching bot strategy: EMA Fast/Slow, RSI, MACD, Volume, ORB range band, and Open Interest.
  - UI conveniences: crosshair stats, zoom/pan, log scale, fullscreen, and branded PNG export.
- **Multi-Source Data Adapters**:
  - `DelayedNSESource`: 15-minute delayed equity quotes via `yfinance` with query coalescing and in-memory TTL caching.
  - `CryptoShowcaseSource`: Live demonstration feed using public Kraken OHLC data (BTC, ETH).
  - `DemoSource`: Synthetic trading day replay.
  - Watchlist persistence in local git-ignored JSON (`data/chart_watchlist.json`).
  - IST-aware session clock with market state announcements (pre-open, open, closing countdown, weekend, holiday).

### Phase C4: Kite Live Feed, SSE Streaming & Offline Simulator
- **Live Feed Service (`LiveFeedService`)**:
  - Implements official `KiteTicker` WebSocket streaming in background thread mode (`threaded=True`), avoiding Twisted reactor restart collisions across reconnects.
  - Subscription management with cap (max 50 instruments), subscribing in `quote` mode by default and `full` mode only for derivatives displaying OI.
  - Auto-reconnect with backoff and historical backfill bridging gaps after network interruptions.
- **Server-Sent Events (SSE) Engine**:
  - Endpoint `/api/chart/stream` provides low-latency tick and candle streaming with same-origin validation.
  - Throttled pushes (maximum 4 updates/sec/symbol) with heartbeats and drop-oldest client queues (max 500 items).
  - Forming candle updates in place in real time on the frontend chart.
- **Honest Badging & Clock Skew Detection**:
  - Green `LIVE` badge displays only while real ticks arrive within fresh threshold; falls back to `Stale since HH:MM:SS` or `Market closed`.
  - Latency calculated against exchange timestamp; warns if local PC clock is out of sync.
- **Full Offline Kite Simulator**:
  - Opt-in via `--simulate-kite` or `SIMULATE_KITE=1`.
  - Generates realistic synthetic equity (`NSE:SIMULATED`) and futures (`NFO:SIMFUT`) ticks with OI.
  - Dev controls (`/api/kite/simulate/disconnect`, `/api/kite/simulate/token-expiry`, `/api/kite/simulate/reconnect`) enabling complete testing of error/reconnect flows without a Zerodha account.

---

## 3. High-Potential Future Development Scopes

### A. Advanced Charting & Multi-Instrument Workstation
1. **Multi-Chart Grid Layout**:
   - Split view (1x2, 2x1, 2x2 grid) allowing traders to monitor index + constituent (e.g. NIFTY 50 vs INFY/RELIANCE) or multiple timeframes of the same symbol (e.g. 1m entry chart alongside 15m trend chart).
2. **Chart Drawing Tools & Technical Annotations**:
   - Persistent drawing tools overlaid on the canvas: Trendlines, Horizontal Support/Resistance lines, Ray lines, and Fibonacci Retracements.
   - Local storage in SQLite (`data/chart_drawings.db`) so annotations persist across sessions.
3. **Historical Alert Markers on Chart**:
   - Fetch historical fired alerts from `/api/alerts` and plot visual Buy (🟢) and Sell (🔴) markers directly onto corresponding candle timestamps, with tooltip details showing agreeing rules.
4. **Additional Indicator Overlays**:
   - **Session VWAP** (Volume-Weighted Average Price) with standard deviation bands.
   - **Bollinger Bands** (20-period SMA ± 2 standard deviations).
   - **SuperTrend** (ATR-based trend filter).
   - **Stochastic RSI** for enhanced momentum timing.
5. **Sectoral & Index Pre-Populated Watchlists**:
   - Pre-configured tabs for NIFTY 50, Bank NIFTY, NIFTY IT, and Auto constituents, selectable with a single click.

### B. Telegram & Notification Power Features
1. **Rich Formatted Messages**:
   - Support `parse_mode="HTML"` or `"MarkdownV2"` with robust escaping, providing bold tickers, formatted price levels, and monospace reason blocks.
2. **Automated Chart Snapshots via Telegram**:
   - When a high-confidence signal fires, render a headless image of the 1m/5m candle setup with indicator overlays and send it as a Telegram photo attachment (`/sendPhoto`).
3. **Interactive Telegram Inbound Commands**:
   - Long-polling bot listener for inbound commands:
     - `/status`: Returns current bot state, connection status, and today's alert count.
     - `/chart <symbol>`: Responds with an on-demand chart snapshot.
     - `/mute <minutes>`: Temporarily silences alerts for a specified duration.
4. **Supergroup Topic Routing & Multi-Recipient Broadcast**:
   - Support `message_thread_id` for routing alerts to specific topics inside Telegram supergroups.
   - Support a comma-delimited list of `TELEGRAM_CHAT_ID`s to broadcast alerts to multiple channels or team members.

### C. Strategy & Evaluator Expansions
1. **Custom Strategy Rule Plugins**:
   - Drop-in rule loader: allow users to place custom Python rule classes in a `user_rules/` folder that auto-register into the evaluation engine without modifying core code.
2. **Multi-Timeframe Confirmation Filter**:
   - An optional strategy rule requiring alignment with higher-timeframe trends (e.g. only take 1m BUY signals if the 15m EMA 50 is upward-sloping).
3. **Options Chain & PCR (Put-Call Ratio) Strategy Rule**:
   - Calculate Put-Call Ratio and Open Interest buildup across strike chains for NIFTY/BANKNIFTY index options.
4. **Exit & Trailing Stop Signals**:
   - Separate signal categories for profit-taking or trailing stop invalidations (e.g. `🔵 EXIT BUY: EMA cross reversed`).

### D. Backtesting & Quantitative Analytics
1. **Full Historical Strategy Backtester**:
   - Fast, vectorized backtest command (`kite-alerts backtest --symbol NSE:NIFTY-FUT --days 30`).
   - Detailed performance metrics: Total PnL, Win Rate, Profit Factor, Max Drawdown, and Sharpe Ratio.
2. **Walk-Forward Parameter Optimizer**:
   - Grid search utility to find optimal parameters (`EMA_FAST`, `RSI_PERIOD`, etc.) across historical market regimes.
3. **Exportable Trade Journal**:
   - Export alert logs and historical performance to CSV, Excel, or interactive HTML reports.

### E. Portability, Deployment & Packaging
1. **Containerized Deployment (Docker Compose)**:
   - Self-contained `Dockerfile` and `docker-compose.yml` for headless deployment on local Linux home servers, Intel NUCs, or Raspberry Pis.
2. **Desktop System Tray Companion**:
   - Lightweight system tray icon (Windows/macOS) indicating connection status, unread alert badges, and quick links to open the browser dashboard.

---

## 4. Current Repository Structure

```
c:\Users\BAKSHI\Downloads\Zerodha_Python_Alert_Bot
├── config/              # Holidays definition and default configs
├── docs/                # Architecture, design, data sources, and live feed documentation
│   ├── data-sources.md  # Detailed reference of delayed, crypto, and live providers
│   ├── live-feed.md     # KiteTicker Twisted design, limits, and recovery protocols
│   ├── ui-design.md     # Full design specification for UI phases C1 through C4
│   └── ROADMAP.md       # Project roadmap, historical builds, and future development scopes
├── legacy/              # Original reference prototype
├── scripts/             # Integrity, golden demo, and import verification scripts
├── src/kite_alerts/     # Core package
│   ├── backtest/        # Replay and forward-return evaluation
│   ├── cli/             # Entry points for `kite-alerts` CLI commands
│   ├── config/          # Pydantic v2 typed settings and per-symbol overrides
│   ├── core/            # IST clock, models, resilience, ports, and trading calendar
│   ├── data/            # Instrument lookup, candle builder, and Kite historical client
│   ├── engine/          # Alert engine, candle aggregator, and evaluation worker
│   ├── notifiers/       # Hub, token-bucket rate limiter, DLQ, Telegram, Slack, SMTP
│   ├── observability/   # Metrics, structured logging, and health endpoints
│   ├── persistence/     # SQLite alert store and audit ledger
│   ├── strategy/        # Rule registry, strategy evaluator, and indicator rules
│   └── ui/              # FastAPI app, chart service, live feed service, and simulator
│       ├── chart/       # Chart endpoints, technical transforms, and data adapters
│       ├── static/      # Lightweight-Charts UI, CSS styles, and vanilla JS app
│       ├── live_feed.py # KiteTicker WebSocket feed and SSE streaming engine
│       ├── simulator.py # Offline Kite market data simulator with dev controls
│       └── server.py    # Uvicorn server launcher
└── tests/               # Comprehensive unit and integration test suite (725 tests, >85% coverage)
```
