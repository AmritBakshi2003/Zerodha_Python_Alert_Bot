# Data sources (C3 chart)

The chart shows market data without pretending it is something it is not. Every
source is fetched by the Python backend; the browser only talks to this
dashboard (`connect-src 'self'`). Delayed NSE or crypto candles never create an
alert: the chart service does not import the engine, the notifier or the alert
store.

## At a glance

| Picker label | Badge | Live? | Open interest | Needs an account? |
|---|---|---|---|---|
| Delayed NSE (default) | `DELAYED` | No, unofficial and delayed | No | No |
| Crypto showcase | `SHOWCASE` | Forming candle, about 1–2 s | No | No |
| Demo replay | `DEMO` | No, synthetic | Yes (synthetic) | No |
| Kite live | `LIVE` / `SIMULATED` | Yes (real ticks) / Synthetic | Yes (NFO futures/options) | Yes (Zerodha account + data plan), or `--simulate-kite` |

`Kite live` becomes enabled when your Kite credentials and active daily access token exist, and the data-plan probe confirms market data is active on your plan. In testing mode (`--simulate-kite`), it uses synthetic in-memory ticks and displays a permanent purple `SIMULATED` badge. See [docs/live-feed.md](live-feed.md) for architecture and subscription limits.

## Delayed NSE

**Library.** [`yfinance`](https://pypi.org/project/yfinance/) 0.2.x, licensed
Apache-2.0, pinned as `yfinance>=0.2.54,<0.3` in both the `ui` and `dev` extras.
It is imported lazily by the adapter, so the headless bot never needs it.

**What it is.** Yahoo Finance's public NSE quotes. It is **unofficial**: Yahoo is
not an exchange feed, the data can be delayed, revised, missing, or blocked. The
page always says so with the amber `DELAYED` badge and a measured delay, not a
hardcoded "15 minutes".

**Mapping table.**

| `SYMBOLS` value | Provider ticker | Display label |
|---|---|---|
| `NSE:RELIANCE` | `RELIANCE.NS` | `RELIANCE` |
| Any other `NSE:<TICKER>` | `<TICKER>.NS` | `<TICKER>` |
| `NSE:NIFTY 50` (or `NSE:NIFTY`) | `^NSEI` | `NIFTY 50` |
| `NSE:BANK NIFTY` (or `NSE:BANKNIFTY`, `NSE:NIFTY BANK`) | `^NSEBANK` | `BANK NIFTY` |
| `NFO:*` | — | **Unsupported**: Yahoo has no free NFO futures/options mapping here |

NFO is refused with a plain-language reason rather than silently remapped. The
OI panel is hidden because this source has no open interest; the page says "No
OI data from this source".

**Limits.** Yahoo caps intraday history: 1-minute data goes back about five
trading days (the adapter requests `5d`), while 5- and 15-minute data use `1mo`.
The chart asks for at most 600 candles. A 3-minute timeframe is always resampled
from 1-minute candles so it matches the 1-minute series. The adapter never
requests a date range that the provider does not support; the latest session is
what the page shows.

**Freshness.** For every payload the backend compares the last candle's true UTC
epoch with the current clock and reports `age_seconds`, `N min old`, and the
measured `delay_seconds`. The UI displays "Latest candle HH:MM IST, N min old"
and "Last checked HH:MM:SS". Outside NSE hours the page says the market is closed
instead of calling old data an error.

**Rate limits and failure.** A 5–10 second server-side cache (8 s default) is
shared through `ProviderCache`: one fetch at a time per symbol/interval
(coalescing with a lock), exponential backoff with jitter, and a circuit breaker
after 3 consecutive failures for 60 seconds. On failure the last good candles
stay visible with "Stale since HH:MM:SS", and the next retry is automatic. Error
messages are generic and never include a provider URL or response body.

## Crypto showcase

**Provider.** [Kraken's public OHLC endpoint](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data):
`https://api.kraken.com/0/public/OHLC` (documented public market data; no API key,
no account, no secret). The adapter sends only fixed allowlisted parameters
(`pair` and `interval`) to that fixed host; a user-supplied URL is impossible.
Kraken documents a public-call rate of roughly one request per second and returns
up to 720 candles, including the forming candle. The dashboard's short cache
(about 1.5 s) and its 2-second live poll stay well below that.

**Pairs.** `BTC -> XBTUSD`, `ETH -> ETHUSD`. The response's provider-specific key
(for example `XXBTZUSD`) is accepted as well as the requested key.

**Timeframes.** 1, 5 and 15 minute are native; 3 minute is resampled from 1 minute.
The forming candle is polled by the backend and pushed to the page every 2
seconds (the page calls `/api/chart/live`; no provider URL reaches the browser).

**Honesty.** The badge is blue `SHOWCASE`, never the NSE badge, and the copy says
"Crypto showcase is not NSE data." ORB is marked unavailable because it is an NSE
session rule; OI says "No OI data from this source".

**Failure.** If Kraken is unreachable or rate-limits, the same cache/backoff/stale
path applies. A failure here must never block Delayed NSE or the Demo.

## Demo replay

`DemoSource` builds the same synthetic day as `kite-alerts demo` with
`kite_alerts.demo.synthetic_day`, labels it `DEMO`, and computes chart indicators
from that frame. It has one symbol, `DEMO:FUT`, and synthetic OI, so the OI panel
can be demonstrated. Nothing is persisted and no alert is produced; the demo page
and the CLI remain the replay path that runs the real engine.

## Kite live

**Service.** `LiveFeedService` under `src/kite_alerts/ui/live_feed.py`. Streams real-time ticks into 1-minute forming candles using `CandleBuilder`, resamples higher timeframes (such as 3m), and pushes throttled events to browser clients over Server-Sent Events (`/api/chart/stream`).

**Honest Badges.** Green `LIVE` appears strictly when real ticks are arriving within a 30-second window during open market hours. Outside market hours, `"Market closed"` is displayed; when delayed or disconnected, amber `"Stale since HH:MM:SS"` or `"LIVE: waiting for ticks"` is shown.

**Simulator.** Started with `kite-alerts ui --simulate-kite`. Renders a permanent purple `SIMULATED` badge and exercises the entire pipeline (equity, futures OI, disconnects, token expiry) offline without Zerodha credentials.

For details, limits, and the Twisted reactor singleton design, see [docs/live-feed.md](live-feed.md).

## Provider terms and rate limits

These are the terms a user is actually accepting by opening the dashboard; the
badges and the local-only design are not a licence.

| Provider | Used for | Terms / documentation | Published limits |
|---|---|---|---|
| Yahoo Finance (via `yfinance`) | Delayed NSE | [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html); [`yfinance`](https://pypi.org/project/yfinance/) is an independent Apache-2.0 library, not a Yahoo product | No documented API, SLA or rate limit for this endpoint. The adapter self-limits: one fetch at a time per symbol/interval, 8 s cache, exponential backoff, 60 s circuit breaker |
| Kraken | Crypto showcase | [Kraken public OHLC docs](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data); [Kraken legal](https://www.kraken.com/legal) | Public market data, no key. Documented public-call budget is roughly one request per second; the dashboard caches for 1.5 s and polls every 2 s |

**Honesty note for Yahoo.** The delayed-NSE path uses the public chart endpoint
behind Yahoo Finance pages. Yahoo does not document or support it as an API for
third-party applications, publishes no SLA, and its terms above are the ones
that apply — the operator must decide whether that use is acceptable in their
jurisdiction. This project never redistributes the data, never sends it to the
notifier or the alert store, and labels every response `DELAYED`. If Yahoo's
terms are not acceptable, use Kite live (your own data plan) or Demo replay.

**Verifying a provider today.** Run the manual smoke script on a networked
machine; it calls the same allowlisted adapters and fails loudly if the payload
shape changed:

```bash
py -3 scripts/smoke_sources.py
py -3 scripts/smoke_sources.py --provider kraken --interval 5
```

It prints candle count, last-candle UTC time and the measured lag, and exits
non-zero on failure. It is deliberately not in CI (no network) and its results
are UNVERIFIED until a human runs it.



Every adapter implements the small `ChartSource` protocol in
`src/kite_alerts/ui/chart/sources.py`:

```python
class ChartSource(Protocol):
    id: str  # stable picker id, e.g. "nse_delayed"
    label: str  # human label
    badge: str  # DELAYED / SHOWCASE / DEMO / LIVE
    supports_oi: bool
    enabled: bool  # False keeps it visible but disabled
    session_based: bool  # False: do not pretend an NSE ORB applies

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]: ...
    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries: ...
```

`fetch` returns normalised `Candle` objects with a true UTC epoch `time` and at
most one `SourceSeries`; it must:

1. accept only allowlisted symbols and intervals (never a URL);
2. raise `SourceError` with a plain message and optional `retry_after`, never a
   provider exception, URL or raw body;
3. go through a `ProviderCache` for caching/backoff;
4. avoid importing the alert engine, notifier or store.

Register it in `default_sources()` and in the picker metadata
(`/api/data-sources`); add it to `docs/data-sources.md` and tests with a fake
fetcher. `ChartService` turns `SourceError` into a 422/503 JSON response and
builds indicators, signal meter, freshness and watchlist rows for every source.

## Watchlist

The list starts from `SYMBOLS`, mapped for the active source. On Delayed NSE the
page can add more `NSE:` symbols; they are stored in
`data/chart_watchlist.json` (git-ignored), atomically, not in `localStorage`.
The browser separately remembers the last source, symbol, timeframe, chart type
and indicator toggles in `localStorage`; no secret is stored there. Rows show the
last price, change %, a small sparkline, freshness, and are cached for 15 seconds
plus the provider cache.

## Kite live (C4, not implemented)

Live Kite data needs the user's **own** Kite Connect app and a market-data plan
(bring-your-own-key). The C2 Connect page already handles the login and data-plan
probe. C4 will add the streaming feed; C3 deliberately does not open a Kite
websocket, poll Kite quotes for the chart, or accept a Kite API key anywhere new.

## Security and alert isolation

- Loopback only, Host-header validation, strict CSP (`connect-src 'self'`), no
  CORS, same-origin checks on watchlist writes.
- Source, symbol, interval and limit are validated against allowlists/regex; the
  backend only contacts fixed provider hosts (SSRF tests prove a URL in a symbol
  field never causes a request).
- Provider errors are not logged with URLs or bodies; logs contain only a concise
  event and exception class.
- No chart code path imports or calls the engine, notifier, alert store or
  history. Tests assert the alert store is untouched and every chart route keeps
  working with alert sending forbidden.
