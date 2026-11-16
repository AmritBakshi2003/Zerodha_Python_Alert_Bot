/* Chart workstation (C3).
 *
 * All candles and indicators come from this dashboard's own Python backend.
 * The browser never contacts a market-data provider. Nothing here can send an
 * alert: this module has no notifier or alert-store call at all.
 */

import { alertRow, api, byId, clear, make, show } from "./dom.js";

const KEYS = {
  source: "kiteAlertsChartSource",
  symbol: "kiteAlertsChartSymbol",
  interval: "kiteAlertsChartInterval",
  type: "kiteAlertsChartType",
  indicators: "kiteAlertsChartIndicators",
  log: "kiteAlertsChartLog",
};

const SOURCE_IDS = ["nse_delayed", "crypto_showcase", "demo", "kite_live"];
const SOURCE_ORDER = { nse_delayed: 0, crypto_showcase: 1, demo: 2, kite_live: 3 };
const INDICATORS = ["ema", "rsi", "macd", "volume", "orb", "oi"];
const FALLBACK_SOURCES = [
  { id: "nse_delayed", label: "Delayed NSE", badge: "DELAYED", enabled: true, notice: "Unofficial delayed data. Not live." },
  { id: "crypto_showcase", label: "Crypto showcase", badge: "SHOWCASE", enabled: true, notice: "Crypto showcase is not NSE data." },
  { id: "demo", label: "Demo replay", badge: "DEMO", enabled: true, notice: "A made-up day for practice." },
  { id: "kite_live", label: "Kite live", badge: "LIVE", enabled: false, reason: "Connect to Zerodha to use live Kite market data.", notice: "Connect to Zerodha to use live Kite market data." },
];
const BADGE_MESSAGES = {
  DELAYED: "DELAYED — unofficial, delayed data",
  SHOWCASE: "SHOWCASE — crypto, not NSE data",
  DEMO: "DEMO — a made-up practice day",
  LIVE: "LIVE",
  SIMULATED: "SIMULATED, not real market data",
};

const state = {
  started: false,
  source: "nse_delayed",
  symbol: "",
  interval: 5,
  chartType: "candles",
  indicators: { ema: true, rsi: true, macd: true, volume: true, orb: true, oi: true },
  log: false,
  sources: [],
  symbols: [],
  payload: null,
  data: [],
  byTime: new Map(),
  chart: null,
  priceSeries: null,
  series: {},
  tooltip: null,
  liveTimer: null,
  stream: null,
  isSimulated: false,
  lastTickTs: null,
  feedStatus: null,
  clockTimer: null,
  statusTimer: null,
  alertTimer: null,
  statusVisible: true,
  market: null,
  clockSeconds: 0,
};

/* --------------------------------------------------------------- persistence */

function stored(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (error) {
    return fallback;
  }
}

function store(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (error) {
    /* Private mode still works; remembering is a convenience, not a requirement. */
  }
}

function loadPreferences() {
  const source = stored(KEYS.source, "nse_delayed");
  state.source = SOURCE_IDS.indexOf(source) >= 0 ? source : "nse_delayed";
  state.symbol = stored(KEYS.symbol, "");
  const interval = Number(stored(KEYS.interval, "5"));
  state.interval = [1, 3, 5, 15].indexOf(interval) >= 0 ? interval : 5;
  const chartType = stored(KEYS.type, "candles");
  state.chartType = ["candles", "hollow", "heikin", "bars", "line", "area", "baseline"].indexOf(chartType) >= 0
    ? chartType : "candles";
  let saved = {};
  try {
    saved = JSON.parse(stored(KEYS.indicators, "{}")) || {};
  } catch (error) {
    saved = {};
  }
  INDICATORS.forEach(function (name) {
    if (typeof saved[name] === "boolean") {
      state.indicators[name] = saved[name];
    }
  });
  state.log = stored(KEYS.log, "false") === "true";
}

function savePreferences() {
  store(KEYS.source, state.source);
  store(KEYS.symbol, state.symbol);
  store(KEYS.interval, state.interval);
  store(KEYS.type, state.chartType);
  store(KEYS.indicators, JSON.stringify(state.indicators));
  store(KEYS.log, state.log ? "true" : "false");
}

/* ------------------------------------------------------------------- helpers */

function isVisible() {
  const page = byId("page-chart");
  return Boolean(page && !page.hidden);
}

function palette() {
  const css = window.getComputedStyle(document.documentElement);
  const read = function (name, fallback) {
    return (css.getPropertyValue(name) || fallback).trim();
  };
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return {
    dark: dark,
    text: read("--text", dark ? "#e8edf2" : "#17202a"),
    muted: read("--muted", dark ? "#a4afba" : "#55616d"),
    panel: read("--panel", dark ? "#161b22" : "#ffffff"),
    border: read("--border", dark ? "#2a323c" : "#d7dde3"),
    up: read("--buy", dark ? "#43d17c" : "#0f7a37"),
    down: read("--sell", dark ? "#ff7b72" : "#b3261e"),
    line: dark ? "#6cb6ff" : "#1f6feb",
    slow: dark ? "#f0a35e" : "#b45309",
    rsi: dark ? "#c792ea" : "#7b3ff2",
    volume: dark ? "#2c3e50" : "#c8d3dd",
    orb: dark ? "#c8a45c" : "#8a6d3b",
  };
}

function istTime(value) {
  if (typeof value !== "number") {
    return "";
  }
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }
  return number.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDelay(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) {
    return "";
  }
  if (value < 90) {
    return Math.round(value) + " s";
  }
  const minutes = Math.round(value / 60);
  if (minutes < 120) {
    return minutes + " min";
  }
  return Math.round(minutes / 60) + " h";
}

function message(text, tone) {
  const node = byId("chart-message");
  node.textContent = text || "";
  node.classList.remove("is-warning", "is-error", "is-ok");
  if (tone) {
    node.classList.add("is-" + tone);
  }
}

/* -------------------------------------------------------------------- status */

function setBotStatus(status) {
  const running = Boolean(status && status.running);
  const dot = byId("chart-bot-dot");
  dot.dataset.state = running ? "running" : "stopped";
  byId("chart-bot-status").textContent = "Bot: " + (status && status.label
    ? status.label.replace(/^Bot\s+/i, "")
    : running ? "running" : "not running");
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    setBotStatus(data.status || {});
    const today = data.today || {};
    byId("chart-count-buys").textContent = String(today.buys || 0);
    byId("chart-count-sells").textContent = String(today.sells || 0);
  } catch (error) {
    setBotStatus({ running: false, label: "dashboard not responding" });
  }
  await refreshAlerts();
}

async function refreshAlerts() {
  try {
    const data = await api("/api/alerts/recent?limit=8");
    const items = data.items || [];
    const feed = byId("chart-alert-feed");
    clear(feed);
    items.forEach(function (item) {
      feed.appendChild(alertRow(item));
    });
    byId("chart-alert-count").textContent = "Alerts: " + items.length;
    byId("chart-alert-summary").textContent = items.length
      ? "newest " + items.length : "none yet";
  } catch (error) {
    /* the status strip already shows that the dashboard is unreachable */
  }
}

/* ------------------------------------------------------------- data sources */

function sourceById(id) {
  return state.sources.filter(function (source) { return source.id === id; })[0] || null;
}

function renderSourceOptions() {
  const select = byId("chart-source");
  clear(select);
  state.sources.forEach(function (source) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = source.label;
    option.disabled = source.enabled === false;
    const reason = source.reason || source.notice || "";
    if (reason) {
      option.title = reason;
    }
    select.appendChild(option);
  });
  const current = sourceById(state.source);
  if (!current || current.enabled === false) {
    const enabled = state.sources.filter(function (source) { return source.enabled !== false; });
    state.source = enabled.length ? enabled[0].id : "nse_delayed";
    savePreferences();
  }
  select.value = state.source;
}

function setSourceNote() {
  const source = sourceById(state.source);
  const note = byId("chart-source-note");
  if (!source) {
    note.textContent = "";
    return;
  }
  const parts = [];
  if (source.notice) {
    parts.push(source.notice);
  }
  if (source.enabled === false && source.reason) {
    parts.push(source.reason);
  }
  if (source.id === "nse_delayed") {
    parts.push("Delayed NSE is fetched by the backend and is not live Kite data.");
  }
  if (source.id === "crypto_showcase") {
    parts.push("Crypto is labelled SHOWCASE so it is never mistaken for NSE data.");
  }
  if (source.id === "kite_live") {
    if (state.isSimulated || source.badge === "SIMULATED") {
      parts.push("SIMULATED, not real market data. Ticks are generated locally.");
    } else {
      parts.push("Streaming live ticks directly from your Kite account.");
    }
  }
  note.textContent = parts.join(" ");
  setBadge(source.badge || "DELAYED");
  if (source.id === "kite_live") {
    updateLiveBadge();
  }
}

function setBadge(badge) {
  const node = byId("chart-badge");
  node.textContent = badge || "DELAYED";
  node.dataset.badge = badge || "DELAYED";
  node.classList.remove("is-waiting", "is-stale");
}

function updateLiveBadge() {
  if (state.source !== "kite_live") {
    return;
  }
  const badge = byId("chart-badge");
  if (!badge) {
    return;
  }

  if (state.isSimulated) {
    badge.textContent = "SIMULATED";
    badge.dataset.badge = "SIMULATED";
    badge.title = "SIMULATED, not real market data";
    badge.classList.remove("is-waiting", "is-stale");
    return;
  }

  const st = state.feedStatus ? state.feedStatus.state : "connecting";
  const now = Date.now();
  const age = state.lastTickTs ? (now - state.lastTickTs) / 1000 : 999;

  if (st === "live") {
    if (age <= 30) {
      badge.textContent = "LIVE";
      badge.dataset.badge = "LIVE";
      badge.title = "Streaming real Kite market data";
      badge.classList.remove("is-waiting", "is-stale");
    } else {
      const since = state.feedStatus && state.feedStatus.last_tick_time ? state.feedStatus.last_tick_time : "";
      badge.textContent = "Stale" + (since ? " since " + since : "");
      badge.dataset.badge = "LIVE";
      badge.classList.remove("is-waiting");
      badge.classList.add("is-stale");
    }
  } else if (st === "connecting") {
    badge.textContent = "LIVE: waiting for ticks";
    badge.dataset.badge = "LIVE";
    badge.classList.remove("is-stale");
    badge.classList.add("is-waiting");
  } else if (st === "market_closed") {
    badge.textContent = "Market closed";
    badge.dataset.badge = "LIVE";
    badge.classList.remove("is-stale");
    badge.classList.add("is-waiting");
  } else if (st === "token_expired") {
    badge.textContent = "Token expired";
    badge.dataset.badge = "DELAYED";
  }
}

async function loadSources() {
  let data = null;
  try {
    data = await api("/api/data-sources");
  } catch (error) {
    data = null;
  }
  state.sources = data && Array.isArray(data.sources) && data.sources.length
    ? data.sources : FALLBACK_SOURCES;
  state.sources.sort(function (left, right) {
    return (SOURCE_ORDER[left.id] || 0) - (SOURCE_ORDER[right.id] || 0);
  });
  state.sources.forEach(function (source) {
    // The server is the only judge of availability; never hard-code it here.
    if (typeof source.enabled !== "boolean") {
      source.enabled = true;
    }
  });
  renderSourceOptions();
  setSourceNote();
}

/* ------------------------------------------------------------------ symbols */

async function loadSymbols() {
  const requestedSource = state.source;
  const select = byId("chart-symbol");
  clear(select);
  let data = null;
  try {
    data = await api("/api/chart/symbols?source=" + encodeURIComponent(requestedSource));
  } catch (error) {
    if (requestedSource === state.source) {
      message(error.message, "error");
    }
    return;
  }
  if (requestedSource !== state.source) {
    return;
  }
  state.symbols = data.symbols || [];
  state.symbols.forEach(function (info) {
    const option = document.createElement("option");
    option.value = info.symbol;
    option.textContent = info.supported ? info.label : info.label + " (unavailable)";
    option.disabled = !info.supported;
    if (info.reason) {
      option.title = info.reason;
    }
    select.appendChild(option);
  });
  const chosen = state.symbols.filter(function (info) {
    return info.symbol === state.symbol && info.supported;
  })[0] || state.symbols.filter(function (info) { return info.supported; })[0];
  state.symbol = chosen ? chosen.symbol : "";
  select.value = state.symbol;
  savePreferences();
}

/* -------------------------------------------------------------------- chart */

function ensureChart() {
  const container = byId("chart-main");
  if (!window.LightweightCharts) {
    message("The chart library did not load. Reload this page.", "error");
    return false;
  }
  const colors = palette();
  if (state.chart) {
    state.chart.remove();
    state.chart = null;
  }
  container.textContent = "";
  state.chart = window.LightweightCharts.createChart(container, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: colors.panel },
      textColor: colors.text,
      fontSize: 12,
    },
    grid: {
      vertLines: { color: colors.border },
      horzLines: { color: colors.border },
    },
    rightPriceScale: { borderColor: colors.border },
    timeScale: {
      borderColor: colors.border,
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: function (time) { return istTime(time); },
    },
    localization: {
      locale: "en-IN",
      timeFormatter: function (time) { return istTime(time); },
    },
    crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
  });
  return true;
}

function basePricePoint(candle) {
  if (state.chartType === "heikin") {
    if (candle.ha_open === null || candle.ha_open === undefined) {
      return null;
    }
    return {
      time: candle.time,
      open: candle.ha_open,
      high: candle.ha_high,
      low: candle.ha_low,
      close: candle.ha_close,
    };
  }
  return {
    time: candle.time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
  };
}

function addPriceSeries(colors) {
  const L = window.LightweightCharts;
  const container = state.chart;
  if (state.chartType === "line") {
    return container.addSeries(L.LineSeries, { color: colors.line, lineWidth: 2 }, 0);
  }
  if (state.chartType === "area") {
    return container.addSeries(L.AreaSeries, {
      lineColor: colors.line,
      topColor: colors.dark ? "rgba(108,182,255,0.28)" : "rgba(31,111,235,0.22)",
      bottomColor: "rgba(31,111,235,0.02)",
      lineWidth: 2,
    }, 0);
  }
  if (state.chartType === "baseline") {
    const base = state.data.length ? state.data[0].close : 0;
    return container.addSeries(L.BaselineSeries, {
      baseValue: { type: "price", price: base },
      topLineColor: colors.up,
      topFillColor1: colors.dark ? "rgba(67,209,124,0.25)" : "rgba(15,122,55,0.20)",
      topFillColor2: "rgba(15,122,55,0.02)",
      bottomLineColor: colors.down,
      bottomFillColor1: colors.dark ? "rgba(255,123,114,0.25)" : "rgba(179,38,30,0.20)",
      bottomFillColor2: "rgba(179,38,30,0.02)",
      lineWidth: 2,
    }, 0);
  }
  if (state.chartType === "bars") {
    return container.addSeries(L.BarSeries, {
      upColor: colors.up,
      downColor: colors.down,
    }, 0);
  }
  const hollow = state.chartType === "hollow";
  return container.addSeries(L.CandlestickSeries, {
    upColor: hollow ? "rgba(0,0,0,0)" : colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
  }, 0);
}

function pricePoint(candle) {
  if (["line", "area", "baseline"].indexOf(state.chartType) >= 0) {
    return { time: candle.time, value: candle.close };
  }
  return basePricePoint(candle);
}

function renderChart() {
  if (!state.payload || !state.data.length) {
    return;
  }
  if (!ensureChart()) {
    return;
  }
  const colors = palette();
  const L = window.LightweightCharts;
  state.series = { orb: [] };
  state.priceSeries = addPriceSeries(colors);
  state.priceSeries.priceScale().applyOptions({
    mode: state.log ? L.PriceScaleMode.Logarithmic : L.PriceScaleMode.Normal,
  });
  state.priceSeries.setData(state.data.map(pricePoint).filter(Boolean));

  if (state.indicators.ema) {
    state.series.emaFast = state.chart.addSeries(L.LineSeries, {
      color: colors.line, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      title: "EMA fast",
    }, 0);
    state.series.emaSlow = state.chart.addSeries(L.LineSeries, {
      color: colors.slow, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      title: "EMA slow",
    }, 0);
    state.series.emaFast.setData(state.data
      .filter(function (candle) { return candle.ema_fast !== null; })
      .map(function (candle) { return { time: candle.time, value: candle.ema_fast }; }));
    state.series.emaSlow.setData(state.data
      .filter(function (candle) { return candle.ema_slow !== null; })
      .map(function (candle) { return { time: candle.time, value: candle.ema_slow }; }));
  }

  let pane = 1;
  if (state.indicators.volume) {
    state.series.volume = state.chart.addSeries(L.HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      lastValueVisible: false,
      priceLineVisible: false,
    }, pane);
    state.series.volume.setData(state.data.map(function (candle) {
      return {
        time: candle.time,
        value: candle.volume || 0,
        color: candle.close >= candle.open ? colors.up : colors.down,
      };
    }));
    pane += 1;
  }

  if (state.indicators.rsi) {
    state.series.rsi = state.chart.addSeries(L.LineSeries, {
      color: colors.rsi, lineWidth: 1, priceLineVisible: false, title: "RSI",
    }, pane);
    state.series.rsi.setData(state.data
      .filter(function (candle) { return candle.rsi !== null; })
      .map(function (candle) { return { time: candle.time, value: candle.rsi }; }));
    [30, 70].forEach(function (level) {
      state.series.rsi.createPriceLine({
        price: level, color: colors.border, lineWidth: 1, lineStyle: L.LineStyle.Dashed,
        axisLabelVisible: true, title: String(level),
      });
    });
    pane += 1;
  }

  if (state.indicators.macd) {
    state.series.macdHist = state.chart.addSeries(L.HistogramSeries, {
      priceLineVisible: false, lastValueVisible: false,
    }, pane);
    state.series.macd = state.chart.addSeries(L.LineSeries, {
      color: colors.line, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      title: "MACD",
    }, pane);
    state.series.macdSignal = state.chart.addSeries(L.LineSeries, {
      color: colors.slow, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      title: "signal",
    }, pane);
    state.series.macdHist.setData(state.data
      .filter(function (candle) { return candle.macd_hist !== null; })
      .map(function (candle) {
        return {
          time: candle.time,
          value: candle.macd_hist,
          color: candle.macd_hist >= 0 ? colors.up : colors.down,
        };
      }));
    state.series.macd.setData(state.data
      .filter(function (candle) { return candle.macd !== null; })
      .map(function (candle) { return { time: candle.time, value: candle.macd }; }));
    state.series.macdSignal.setData(state.data
      .filter(function (candle) { return candle.macd_signal !== null; })
      .map(function (candle) { return { time: candle.time, value: candle.macd_signal }; }));
    pane += 1;
  }

  if (state.indicators.oi && state.payload.oi && state.payload.oi.available) {
    const oiPoints = state.data
      .filter(function (candle) { return candle.oi !== null && candle.oi !== undefined; })
      .map(function (candle) { return { time: candle.time, value: candle.oi }; });
    if (oiPoints.length) {
      state.series.oi = state.chart.addSeries(L.LineSeries, {
        color: colors.orb, lineWidth: 1, priceLineVisible: false, title: "OI",
      }, pane);
      state.series.oi.setData(oiPoints);
    }
  }
  const oiNote = byId("chart-oi-note");
  if (oiNote) {
    oiNote.hidden = !(state.indicators.oi && state.payload.oi && state.payload.oi.available);
  }

  if (state.indicators.orb && state.payload.orb) {
    [["ORB high", state.payload.orb.high], ["ORB low", state.payload.orb.low]].forEach(
      function (entry) {
        state.series.orb.push(state.priceSeries.createPriceLine({
          price: entry[1],
          color: colors.orb,
          lineWidth: 1,
          lineStyle: L.LineStyle.Dashed,
          axisLabelVisible: true,
          title: entry[0],
        }));
      }
    );
  }

  const panes = state.chart.panes();
  for (let index = 1; index < panes.length; index += 1) {
    panes[index].setHeight(110);
  }
  state.chart.timeScale().fitContent();
  bindTooltip();
}

/* ------------------------------------------------------------------ tooltip */

function bindTooltip() {
  if (!state.chart || !state.priceSeries) {
    return;
  }
  state.chart.subscribeCrosshairMove(function (param) {
    const tip = byId("chart-tooltip");
    if (!param || !param.time || !param.point) {
      show(tip, false);
      return;
    }
    const candle = state.byTime.get(param.time);
    if (!candle) {
      show(tip, false);
      return;
    }
    clear(tip);
    tip.appendChild(make("strong", null,
      candle.ist + " IST · " + state.payload.symbol_label));
    tip.appendChild(make("div", null,
      "O " + formatPrice(candle.open) + "  H " + formatPrice(candle.high)));
    tip.appendChild(make("div", null,
      "L " + formatPrice(candle.low) + "  C " + formatPrice(candle.close)));
    tip.appendChild(make("div", "muted small",
      "Vol " + Number(candle.volume || 0).toLocaleString("en-IN")));
    if (candle.ema_fast !== null) {
      tip.appendChild(make("div", "muted small", "EMA fast " + formatPrice(candle.ema_fast)));
    }
    if (candle.rsi !== null) {
      tip.appendChild(make("div", "muted small", "RSI " + formatPrice(candle.rsi)));
    }
    const stage = byId("chart-main").parentElement.getBoundingClientRect();
    let left = param.point.x + 16;
    let top = param.point.y + 16;
    if (left + 220 > stage.width) {
      left = Math.max(4, param.point.x - 220);
    }
    if (top + 140 > stage.height) {
      top = Math.max(4, param.point.y - 140);
    }
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    show(tip, true);
  });
}

/* ---------------------------------------------------------------- freshness */

function renderFreshness() {
  const payload = state.payload;
  if (!payload || !payload.freshness) {
    return;
  }
  const fresh = payload.freshness;
  byId("chart-freshness").textContent =
    "Latest candle " + fresh.latest_time + " IST, " + fresh.age_text + ".";
  byId("chart-checked").textContent = "Last checked " + fresh.checked_at + ".";
  let delay = "";
  if (payload.source === "nse_delayed" && fresh.delay_seconds !== null) {
    delay = "Measured provider delay: about " + formatDelay(fresh.delay_seconds) +
      " behind the candle timestamp. Yahoo Finance is unofficial and can be delayed.";
  } else if (payload.source === "crypto_showcase") {
    delay = "The forming candle is refreshed from the backend about every 2 seconds.";
  } else if (payload.source === "demo") {
    delay = "Synthetic practice data. It is not a market feed.";
  }
  byId("chart-delay").textContent = delay;

  const stale = byId("chart-stale");
  if (fresh.stale) {
    stale.textContent = "Stale since " + (fresh.stale_since || fresh.checked_at) +
      ". Keeping the last data visible while the provider is retried.";
    show(stale, true);
  } else {
    show(stale, false);
  }
  message(payload.message || "", fresh.stale ? "warning" : "");
}

function renderSignal() {
  const payload = state.payload;
  if (!payload || !payload.signal) {
    return;
  }
  const signal = payload.signal;
  byId("signal-summary").textContent = signal.summary;
  byId("signal-label").textContent = signal.label;
  const list = byId("signal-rules");
  clear(list);
  (signal.rules || []).forEach(function (rule) {
    const item = make("li", "signal-rule signal-" + rule.status);
    const mark = rule.status === "bullish" ? "▲"
      : rule.status === "bearish" ? "▼"
        : rule.status === "neutral" ? "•" : "—";
    item.appendChild(make("span", "signal-mark", mark));
    item.appendChild(make("span", "signal-name", rule.label));
    item.appendChild(make("span", "signal-state", rule.state));
    list.appendChild(item);
  });
}

/* -------------------------------------------------------------------- watchlist */

function sparkline(values, rising) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 80 24");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("aria-hidden", "true");
  if (!values || values.length < 2) {
    return svg;
  }
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const span = max - min || 1;
  const points = values.map(function (value, index) {
    const x = (index / (values.length - 1)) * 80;
    const y = 22 - ((value - min) / span) * 20;
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", rising ? "var(--buy)" : "var(--sell)");
  line.setAttribute("stroke-width", "1.5");
  svg.appendChild(line);
  return svg;
}

function renderWatchlist(data) {
  const list = byId("watchlist");
  clear(list);
  const items = data.items || [];
  const empty = byId("watchlist-empty");
  show(empty, items.length === 0);
  if (!items.length) {
    empty.textContent = "No symbols for this source.";
  }
  items.forEach(function (row) {
    const item = document.createElement("li");
    item.className = "watch-row";
    const load = document.createElement("button");
    load.type = "button";
    load.className = "watch-load";
    load.appendChild(make("span", "watch-symbol", row.label || row.symbol));
    if (row.supported && Number.isFinite(Number(row.last))) {
      load.appendChild(make("span", "watch-price", formatPrice(row.last)));
      const change = Number(row.change_pct);
      load.appendChild(make(
        "span",
        "watch-change " + (change >= 0 ? "is-up" : "is-down"),
        (change >= 0 ? "+" : "") + change.toFixed(2) + "%",
      ));
      load.appendChild(sparkline(row.sparkline, change >= 0));
      load.appendChild(make("span", "watch-age muted small", row.age_text || ""));
    } else {
      load.disabled = true;
      load.appendChild(make("span", "watch-reason muted small",
        row.reason || "Unavailable right now."));
    }
    load.addEventListener("click", function () {
      if (!row.supported) {
        return;
      }
      state.symbol = row.symbol;
      byId("chart-symbol").value = row.symbol;
      savePreferences();
      loadChart();
    });
    item.appendChild(load);
    if (state.source === "nse_delayed" && row.removable) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "watch-remove";
      remove.textContent = "×";
      remove.title = "Remove this added symbol";
      remove.setAttribute("aria-label", "Remove " + row.symbol + " from the watchlist");
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        removeWatchlist(row.symbol);
      });
      item.appendChild(remove);
    }
    list.appendChild(item);
  });
  show(byId("watchlist-add"), state.source === "nse_delayed");
  byId("watchlist-status").textContent = "Updated " + (data.checked_at || "") + ".";
}

async function loadWatchlist() {
  const requestedSource = state.source;
  try {
    const data = await api("/api/chart/watchlist?source=" + encodeURIComponent(requestedSource));
    if (requestedSource !== state.source) {
      return;
    }
    renderWatchlist(data);
  } catch (error) {
    if (requestedSource === state.source) {
      byId("watchlist-status").textContent = error.message;
    }
  }
}

async function addWatchlist() {
  const input = byId("watchlist-symbol");
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) {
    byId("watchlist-status").textContent = "Type an NSE symbol such as NSE:INFY.";
    return;
  }
  try {
    renderWatchlist(await api("/api/chart/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: state.source, symbol: symbol }),
    }));
    input.value = "";
  } catch (error) {
    byId("watchlist-status").textContent = error.message;
  }
}

async function removeWatchlist(symbol) {
  try {
    renderWatchlist(await api("/api/chart/watchlist", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: state.source, symbol: symbol }),
    }));
  } catch (error) {
    byId("watchlist-status").textContent = error.message;
  }
}

/* ---------------------------------------------------------------------- clock */

function formatCountdown(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  return (hours ? hours + "h " : "") + minutes + "m " + String(secs).padStart(2, "0") + "s";
}

function renderClockText() {
  const market = state.market;
  if (!market) {
    return;
  }
  byId("clock-state").textContent = market.label;
  byId("clock-next").textContent = market.is_open
    ? "Closes in " + formatCountdown(state.clockSeconds)
    : "Opens in " + formatCountdown(state.clockSeconds) + " (" + market.next_event_at + " IST)";
  byId("clock-session").textContent = "Session " + market.session_open + "–" +
    market.session_close + " IST · now " + market.now_ist;
  const notice = byId("clock-notice");
  notice.textContent = market.notice || "";
  show(notice, Boolean(market.notice));
  byId("chart-clock-strip").textContent = "NSE: " + market.label;
}

function renderClock(data) {
  if (!data || !data.market) {
    return;
  }
  state.market = data.market;
  state.clockSeconds = Number(data.market.countdown_seconds) || 0;
  renderClockText();
}

async function refreshClock() {
  try {
    renderClock(await api("/api/chart/clock"));
  } catch (error) {
    byId("clock-state").textContent = "Clock unavailable";
  }
}

/* ----------------------------------------------------------------------- data */

function setData(payload) {
  state.payload = payload;
  state.data = payload.candles || [];
  state.byTime = new Map();
  state.data.forEach(function (candle) {
    state.byTime.set(candle.time, candle);
  });
  setBadge(payload.badge);
  renderChart();
  renderFreshness();
  renderSignal();
}

async function loadChart() {
  if (!state.symbol) {
    return;
  }
  const requested = { source: state.source, symbol: state.symbol, interval: state.interval };
  message("Loading candles…");
  const params = new URLSearchParams();
  params.set("source", requested.source);
  params.set("symbol", requested.symbol);
  params.set("interval", String(requested.interval));
  params.set("limit", "300");
  try {
    const payload = await api("/api/chart/candles?" + params.toString());
    if (requested.source !== state.source || requested.symbol !== state.symbol
        || requested.interval !== state.interval) {
      return;
    }
    setData(payload);
    state.clockSeconds = payload.market ? Number(payload.market.countdown_seconds) || 0 : state.clockSeconds;
    renderClock({ market: payload.market });
  } catch (error) {
    message(error.message, "error");
    if (state.payload) {
      const stale = byId("chart-stale");
      stale.textContent = "Showing the last data received at " +
        (state.payload.freshness ? state.payload.freshness.checked_at : "an earlier time") +
        ". Retrying automatically.";
      show(stale, true);
    }
  }
  await loadWatchlist();
  startLivePolling();
}

function updateLivePoint(candle) {
  state.byTime.set(candle.time, candle);
  const last = state.data[state.data.length - 1];
  if (last && last.time === candle.time) {
    state.data[state.data.length - 1] = candle;
  } else if (!last || candle.time > last.time) {
    state.data.push(candle);
  }
  const point = pricePoint(candle);
  if (point) {
    state.priceSeries.update(point);
  }
  if (state.series.volume) {
    state.series.volume.update({
      time: candle.time,
      value: candle.volume || 0,
      color: candle.close >= candle.open ? palette().up : palette().down,
    });
  }
  [["emaFast", "ema_fast"], ["emaSlow", "ema_slow"], ["rsi", "rsi"],
    ["macd", "macd"], ["macdSignal", "macd_signal"], ["macdHist", "macd_hist"],
    ["oi", "oi"]].forEach(function (pair) {
    const series = state.series[pair[0]];
    const value = candle[pair[1]];
    if (series && value !== null && value !== undefined) {
      series.update({ time: candle.time, value: value });
    }
  });
}

async function pollLive() {
  if (!isVisible() || !state.payload || state.source === "demo") {
    return;
  }
  const params = new URLSearchParams();
  params.set("source", state.source);
  params.set("symbol", state.symbol);
  params.set("interval", String(state.interval));
  try {
    const data = await api("/api/chart/live?" + params.toString());
    if (data.source !== state.source || data.symbol !== state.symbol) {
      return;
    }
    const latest = data.candles[data.candles.length - 1];
    const last = state.data[state.data.length - 1];
    if (!latest) {
      return;
    }
    if (!last || latest.time > last.time) {
      await loadChart();
      return;
    }
    state.payload.freshness = data.freshness;
    state.payload.signal = data.signal;
    state.payload.message = data.message;
    state.payload.stale = data.stale;
    updateLivePoint(latest);
    renderFreshness();
    renderSignal();
    renderClock({ market: data.market });
  } catch (error) {
    message(error.message, "warning");
  }
}

function stopLiveStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

function renderFeedStatus(data) {
  const bar = byId("live-feed-bar");
  if (!bar) return;
  bar.hidden = state.source !== "kite_live";
  if (state.source !== "kite_live") return;

  const dot = byId("live-feed-dot");
  if (dot) dot.dataset.state = data.state || "disconnected";
  const stText = byId("live-feed-state");
  if (stText) stText.textContent = "Feed: " + (data.state || "disconnected");
  const syms = byId("live-feed-symbols");
  if (syms) syms.textContent = "Subscribed: " + (data.subscribed || 1);
  const reconnects = byId("live-feed-reconnects");
  if (reconnects) reconnects.textContent = "Reconnects: " + (data.reconnect_count || 0);

  const simControls = byId("sim-controls");
  if (simControls) {
    simControls.hidden = !state.isSimulated;
  }
}

function renderTickLatency(data) {
  const node = byId("live-feed-latency");
  if (!node) return;
  if (data.latency_ms !== null && data.latency_ms !== undefined) {
    const ms = Math.max(1, Math.round(data.latency_ms));
    node.textContent = "Latency: " + ms + " ms";
  } else if (data.latency && data.latency.indexOf("sync") >= 0) {
    node.textContent = "Latency: PC clock may be out of sync";
  } else {
    node.textContent = "Latency: " + (data.latency || "—");
  }
  const ttNode = byId("live-feed-tick-time");
  if (ttNode && data.tick_time) {
    ttNode.textContent = "Last tick: " + data.tick_time + " IST";
  }
}

function startLiveStream() {
  stopLiveStream();
  if (state.source !== "kite_live" || !state.symbol) {
    renderFeedStatus({ state: "disconnected" });
    return;
  }
  renderFeedStatus({ state: "connecting" });
  updateLiveBadge();

  const url = "/api/chart/stream?source=kite_live&symbol=" + encodeURIComponent(state.symbol) + "&interval=" + state.interval;
  const stream = new EventSource(url);
  state.stream = stream;

  stream.addEventListener("status", function (e) {
    try {
      const data = JSON.parse(e.data);
      state.feedStatus = data;
      renderFeedStatus(data);
      updateLiveBadge();
      if (data.state === "token_expired") {
        message(data.reason || "Token expired. Reconnect on the Connect page.", "error");
        state.source = "nse_delayed";
        savePreferences();
        stopLiveStream();
        loadSources().then(function () {
          loadSymbols().then(loadChart);
        });
      }
    } catch (err) {}
  });

  stream.addEventListener("tick", function (e) {
    try {
      const data = JSON.parse(e.data);
      state.lastTickTs = Date.now();
      renderTickLatency(data);
      updateLiveBadge();
    } catch (err) {}
  });

  stream.addEventListener("forming", function (e) {
    try {
      const c = JSON.parse(e.data);
      updateLivePoint(c);
      if (state.payload && state.payload.freshness) {
        state.payload.freshness.latest_time = c.ist ? c.ist.split(" ")[1] : state.payload.freshness.latest_time;
        renderFreshness();
      }
    } catch (err) {}
  });

  stream.addEventListener("candle", function (e) {
    try {
      const c = JSON.parse(e.data);
      updateLivePoint(c);
      loadChart();
    } catch (err) {}
  });

  stream.addEventListener("gap", function () {
    message("Data gap detected and backfilled from history.", "warning");
  });

  stream.onerror = function () {
    const dot = byId("live-feed-dot");
    if (dot) dot.dataset.state = "disconnected";
    const stText = byId("live-feed-state");
    if (stText) stText.textContent = "Feed: reconnecting…";
  };
}

function startLivePolling() {
  if (state.liveTimer !== null) {
    window.clearInterval(state.liveTimer);
    state.liveTimer = null;
  }
  if (state.source === "kite_live") {
    startLiveStream();
    return;
  }
  stopLiveStream();
  const bar = byId("live-feed-bar");
  if (bar) bar.hidden = true;

  const interval = state.source === "crypto_showcase" ? 2000
    : state.source === "nse_delayed" ? 10000 : 0;
  if (!interval) {
    return;
  }
  state.liveTimer = window.setInterval(pollLive, interval);
}

/* ---------------------------------------------------------------------- export */

function exportPng() {
  if (!state.chart || !state.payload) {
    message("Load a chart before exporting.", "warning");
    return;
  }
  const canvas = state.chart.takeScreenshot();
  const context = canvas.getContext("2d");
  const colors = palette();
  const badge = state.payload.badge || "DELAYED";
  context.fillStyle = colors.panel;
  context.fillRect(0, 0, canvas.width, 54);
  context.fillStyle = colors.text;
  context.font = "bold 20px 'Segoe UI', system-ui, sans-serif";
  context.fillText(
    badge + " · " + state.payload.symbol_label + " · " + state.interval + "m · " +
    (state.payload.source_label || ""),
    14, 24,
  );
  context.font = "13px 'Segoe UI', system-ui, sans-serif";
  context.fillStyle = colors.muted;
  context.fillText(BADGE_MESSAGES[badge] || badge, 14, 43);
  context.fillText(
    "Latest candle " + state.payload.freshness.latest_time + " IST · exported " +
    new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }),
    canvas.width - 420, 43,
  );
  const link = document.createElement("a");
  link.download = "kite-chart-" + state.source + "-" + state.symbol.replace(/[^A-Za-z0-9]+/g, "-") + ".png";
  link.href = canvas.toDataURL("image/png");
  link.click();
}

/* ------------------------------------------------------------------------ init */

function applyPreferencesToControls() {
  byId("chart-interval").value = String(state.interval);
  byId("chart-type").value = state.chartType;
  byId("chart-log").checked = state.log;
  document.querySelectorAll("[data-indicator]").forEach(function (input) {
    input.checked = state.indicators[input.dataset.indicator] !== false;
  });
}

function bindControls() {
  byId("chart-source").addEventListener("change", async function (event) {
    state.source = event.target.value;
    savePreferences();
    setSourceNote();
    await loadSymbols();
    await loadChart();
    await loadWatchlist();
    startLivePolling();
  });
  byId("chart-symbol").addEventListener("change", function (event) {
    state.symbol = event.target.value;
    savePreferences();
    loadChart();
  });
  byId("chart-interval").addEventListener("change", function (event) {
    state.interval = Number(event.target.value);
    savePreferences();
    loadChart();
    startLivePolling();
  });
  byId("chart-type").addEventListener("change", function (event) {
    state.chartType = event.target.value;
    savePreferences();
    renderChart();
  });
  byId("chart-log").addEventListener("change", function (event) {
    state.log = event.target.checked;
    savePreferences();
    if (state.priceSeries) {
      state.priceSeries.priceScale().applyOptions({
        mode: state.log ? window.LightweightCharts.PriceScaleMode.Logarithmic
          : window.LightweightCharts.PriceScaleMode.Normal,
      });
    }
  });
  document.querySelectorAll("[data-indicator]").forEach(function (input) {
    input.addEventListener("change", function (event) {
      state.indicators[event.target.dataset.indicator] = event.target.checked;
      savePreferences();
      renderChart();
    });
  });
  byId("chart-export").addEventListener("click", exportPng);
  byId("chart-fullscreen").addEventListener("click", function () {
    const panel = byId("chart-panel");
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (panel.requestFullscreen) {
      panel.requestFullscreen();
    }
  });
  document.addEventListener("fullscreenchange", function () {
    byId("chart-fullscreen").textContent = document.fullscreenElement
      ? "Exit fullscreen" : "Fullscreen";
    if (state.chart) {
      state.chart.resize(byId("chart-main").clientWidth, byId("chart-main").clientHeight);
    }
  });
  byId("watchlist-add-button").addEventListener("click", addWatchlist);
  byId("watchlist-symbol").addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      addWatchlist();
    }
  });

  const btnDisc = byId("sim-btn-disconnect");
  if (btnDisc) {
    btnDisc.addEventListener("click", async function () {
      try {
        await api("/api/kite/simulate/disconnect", { method: "POST" });
        message("Simulator disconnect triggered (will auto-reconnect).", "warning");
      } catch (err) {
        message(err.message, "error");
      }
    });
  }
  const btnExp = byId("sim-btn-expiry");
  if (btnExp) {
    btnExp.addEventListener("click", async function () {
      try {
        await api("/api/kite/simulate/token-expiry", { method: "POST" });
        message("Simulator token expiry triggered.", "warning");
      } catch (err) {
        message(err.message, "error");
      }
    });
  }
  const btnRec = byId("sim-btn-reconnect");
  if (btnRec) {
    btnRec.addEventListener("click", async function () {
      try {
        await api("/api/kite/simulate/reconnect", { method: "POST" });
        message("Simulator reconnected.", "ok");
      } catch (err) {
        message(err.message, "error");
      }
    });
  }

  if (window.matchMedia) {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = function () {
      if (state.payload) {
        renderChart();
      }
    };
    if (media.addEventListener) {
      media.addEventListener("change", listener);
    } else if (media.addListener) {
      media.addListener(listener);
    }
  }
}

function startTimers() {
  if (state.clockTimer === null) {
    state.clockTimer = window.setInterval(function () {
      if (!isVisible()) {
        return;
      }
      state.clockSeconds = Math.max(0, state.clockSeconds - 1);
      renderClockText();
    }, 1000);
  }
  if (state.statusTimer === null) {
    state.statusTimer = window.setInterval(function () {
      if (isVisible()) {
        refreshStatus();
      }
    }, 10000);
  }
  if (state.alertTimer === null) {
    state.alertTimer = window.setInterval(function () {
      if (isVisible()) {
        refreshClock();
      }
    }, 30000);
  }
}

async function initChart() {
  if (state.started) {
    return;
  }
  state.started = true;
  try {
    const about = await api("/api/about");
    if (about && about.simulate_kite) {
      state.isSimulated = true;
    }
  } catch (err) {}
  loadPreferences();
  applyPreferencesToControls();
  bindControls();
  startTimers();
  await loadSources();
  await refreshStatus();
  await refreshClock();
  await loadSymbols();
  await loadChart();
  await loadWatchlist();
  startLivePolling();
}

export async function onShowChart() {
  if (!state.started) {
    await initChart();
    return;
  }
  await refreshStatus();
  await refreshClock();
  if (!state.payload) {
    await loadSymbols();
    await loadChart();
  } else {
    await loadWatchlist();
    startLivePolling();
  }
}
