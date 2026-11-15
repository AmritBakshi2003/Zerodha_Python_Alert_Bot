# Kite Live Feed Architecture & Verification Guide (Phase C4)

This document describes the design, operational limits, resilience mechanisms, and verification procedures for the Kite live chart feed in `kite-alerts`.

---

## 1. Safety & Alerts Isolation Guarantee

* **ALERTS ONLY**: The live chart feed never places, modifies, or cancels orders. It does not touch positions, holdings, or funds.
* **Separation of Concerns**: The live chart feed is purely a visualization component. It **never** calls the notifier, never writes to the alert database (`AlertStore`), and never feeds the alert engine.
* **Alert Engine Independence**: The alert bot (`kite-alerts run`) remains the single source of alerts.

---

## 2. Twisted Reactor Architecture

### The Problem
`kiteconnect.KiteTicker` is built on Twisted's event-driven reactor. In Twisted, `reactor.run()` starts the event loop, and calling `reactor.stop()` permanently terminates the reactor for the entire process: Twisted reactors **cannot be restarted** (`twisted.internet.error.ReactorNotRestartable`).

### The Solution: Long-Lived Singleton Pattern
To support dynamic start, stop, reconnect, and token-refresh without restarting the dashboard process:
1. `LiveFeedService` manages a single `KiteTicker` instance in a dedicated background worker thread (`connect(threaded=True)`).
2. When the feed is stopped or disconnected, the service calls `ticker.close()`, which closes the underlying WebSocket connection and halts retry timers **without stopping the Twisted reactor**.
3. When the user reconnects with a fresh token, a new `KiteTicker` is initialized and connected on the existing reactor.
4. On application shutdown (via FastAPI `lifespan`), all WebSocket connections and SSE queues are cleanly drained and closed.

---

## 3. Documented Limits & Quotas

| Resource | Documented Limit | `kite-alerts` Implementation |
|---|---|---|
| **Simultaneous Connections** | 1 active connection per API key (extra connections disconnect earlier ones) | Exactly **1** shared ticker connection per dashboard instance. |
| **Subscribed Tokens** | Maximum 3,000 tokens per connection | Capped at **25 tokens** (chart symbol + watchlist). Excess symbols are ignored. |
| **Subscription Mode** | `LTP`, `QUOTE` (price + volume), `FULL` (includes OI and market depth) | Uses **`QUOTE` mode** for equities (cheaper bandwidth); uses **`FULL` mode** only for futures/options where the OI panel is enabled. |
| **Historical Data Requests** | ~3 requests per second | Bootstrap historical calls reuse `data/history.py` with exponential backoff and rate-limiting. |
| **SSE Browser Clients** | Server memory bound | Capped at **50 concurrent browser streams**; rejected with HTTP 429 if exceeded. |
| **SSE Queue Buffer** | Bounded buffer | **500 events** max per client; uses **drop-oldest** eviction so slow or suspended browser tabs cannot grow memory. |
| **Browser Throttling** | Refresh rate limit | Ticks pushed to browser are throttled to at most **4 pushes/sec/symbol**. |

---

## 4. Connection Lifecycle & Resilience

```
               [Credentials + Token]
                         │
                         ▼
                 ┌───────────────┐
                 │  Connecting   │
                 └───────┬───────┘
                         │ (WebSocket established)
                         ▼
                 ┌───────────────┐
      ┌─────────►│     Live      ├─────────┐
      │          └───────┬───────┘         │
      │                  │                 │
(Auto-reconnect)  (No ticks /        (Token rejected)
      │            Closed Market)          │
      │                  │                 │
      │                  ▼                 ▼
 ┌────┴────────┐ ┌───────────────┐ ┌───────────────┐
 │Reconnecting │ │Stale / Closed │ │ Token Expired │
 └─────────────┘ └───────────────┘ └───────┬───────┘
                                           │
                                  (Fallback to Delayed)
                                           │
                                           ▼
                                  ┌─────────────────┐
                                  │   Delayed NSE   │
                                  └─────────────────┘
```

1. **Automatic Reconnect**: If the WebSocket drops unexpectedly, `LiveFeedService` applies exponential backoff ($1.0\text{s} \to 60\text{s}$ with jitter).
2. **Gap Backfill**: After a reconnect, the service queries historical 1m data for the disconnected period to bridge any missing candles before resuming live tick updates.
3. **Token Expiry Mid-Session**: If Zerodha returns a 403 or authentication failure, the reconnect loop **immediately stops**, the feed transitions to `STATE_TOKEN_EXPIRED`, a visible notification is presented on the UI, and the chart seamlessly falls back to Delayed NSE.
4. **Market Hours Awareness**: Outside NSE trading hours (9:15 AM – 3:30 PM IST on trading days), ticks stop arriving normally. The UI displays amber `"Market closed"` rather than an alarming error state.

---

## 5. Honest Badges & Latency Monitoring

The dashboard adheres to strict badge truthfulness:

| State | Badge Text | CSS Class / Color | Condition |
|---|---|---|---|
| **Live Streaming** | `LIVE` | Green (`#00c853`) | Real ticks arriving within 30s during active NSE market hours. |
| **Awaiting Data** | `LIVE: waiting for ticks` | Amber (`#ff9800`) | Connected, market open, awaiting first tick. |
| **Market Closed** | `Market closed` | Amber (`#ff9800`) | Connected, outside NSE market hours or on a holiday. |
| **Feed Stale** | `Stale since HH:MM:SS` | Amber (`#ff9800`) | Market is open but no ticks received for $>30\text{s}$. |
| **Token Expired** | `Token expired` | Red / Amber | Token invalid or rejected mid-session. |
| **Simulated Mode** | `SIMULATED` | Permanent Purple (`#7c4dff`) | Started with `--simulate-kite`. Never shows `LIVE`. |

### Feed Latency & Clock Skew
* **Feed Latency**: Computed as $\text{receipt\_time} - \text{exchange\_timestamp}$ (in milliseconds).
* **Clock Skew Protection**: If local PC clock is out of sync (apparent latency is negative or $>300\text{s}$), the UI displays *"Your PC clock may be out of sync"* instead of printing misleading negative or zero latency numbers.

---

## 6. Kite Simulator Mode

To enable full offline testing without a paid Zerodha plan or active trading session:

### Launch Command
```bash
kite-alerts ui --simulate-kite
# or via environment variable:
SIMULATE_KITE=1 kite-alerts ui
```

### Safety Guarantees
* **Zero Network Calls**: Uses completely in-memory synthetic tick generation.
* **Permanent Purple Badge**: The `SIMULATED, not real market data` badge is displayed at all times.
* **Never Counted as Connected**: `/api/kite/session` reports `connected: false`.
* **Isolated from Alerts**: Simulator symbols (`NSE:SIMULATED`, `NFO:SIMFUT`) are never monitored by the alert bot.
* **Dev Controls**:
  - `POST /api/kite/simulate/disconnect`: Simulates connection loss and automatic reconnect backoff.
  - `POST /api/kite/simulate/token-expiry`: Simulates mid-session auth token expiration.
  - `POST /api/kite/simulate/reconnect`: Resumes live simulation.

---

## 7. What Is UNVERIFIED (Honest Checklist)

Because live market data requires an active Zerodha account with a paid Kite Connect subscription during Indian market hours (09:15 - 15:30 IST):

- [ ] **UNVERIFIED**: Ticker behavior against live Zerodha binary WebSocket frames under heavy market volatility (e.g., market open 09:15-09:20 IST).
- [ ] **UNVERIFIED**: Exact latency behavior on networks with strict corporate proxies or deep packet inspection.
- [ ] **UNVERIFIED**: Historical 1m backfill performance during multi-minute ISP disconnects on active session.

---

## 8. First Live Session Verification Checklist

When opening your first live trading session with your real Kite Connect API credentials:

1. **Prerequisites**:
   - Valid `KITE_API_KEY` and `KITE_API_SECRET` in `.env`.
   - Complete daily login via `http://127.0.0.1:8765` (Connect page).
   - Ensure your Kite Connect app has an active market data subscription.
2. **Steps**:
   - Start dashboard: `kite-alerts ui`
   - Navigate to the **Chart** tab.
   - In the **Data Source** picker, select **Kite live**.
   - Verify green `LIVE` badge appears.
   - Verify forming candle updates every second as ticks arrive.
   - For futures symbols (e.g. `NFO:NIFTY26NOVFUT`), verify the **Open Interest (OI)** panel renders below the chart with the note *"OI refreshes less frequently than price"*.
   - Check the **Live feed** status bar at the bottom: verify latency is reported (e.g. `~45 ms`) and symbol count matches active chart + watchlist.
