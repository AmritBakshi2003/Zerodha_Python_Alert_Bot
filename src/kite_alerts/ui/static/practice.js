/* Practice page: the demo player, the first-run welcome and "test my settings".
 *
 * The replay is in-memory on the server; nothing here can write to the alert
 * history, and every alert card is labelled DEMO.
 */

import { api, byId, clear, formatSeconds, make, reasonText, show, sideClass } from "./dom.js";

const SPEED_INTERVALS = { "1": 350, "5": 70, "20": 18, "60": 8 };
const WELCOME_KEY = "kiteAlertsWelcomeDismissed";

const demo = {
  timeline: null,
  chart: null,
  series: null,
  markers: null,
  markerList: [],
  orbLines: [],
  revealedAlerts: [],
  index: 0,
  timer: null,
  playing: false,
  done: false,
  orbShown: false,
};

let started = false;

function palette() {
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark) {
    return {
      background: "#161b22", text: "#e8edf2", grid: "#242c36", border: "#2a323c",
      up: "#43d17c", down: "#ff7b72", emaFast: "#6cb6ff", emaSlow: "#f0a35e",
      rsi: "#c792ea", macd: "#6cb6ff", signal: "#f0a35e",
      histUp: "rgba(67,209,124,0.45)", histDown: "rgba(255,123,114,0.45)",
      orb: "#c8a45c", buy: "#43d17c", sell: "#ff7b72",
    };
  }
  return {
    background: "#ffffff", text: "#17202a", grid: "#eceff2", border: "#d8dde3",
    up: "#0f7a37", down: "#b3261e", emaFast: "#1f6feb", emaSlow: "#b45309",
    rsi: "#7b3ff2", macd: "#1f6feb", signal: "#b45309",
    histUp: "rgba(15,122,55,0.35)", histDown: "rgba(179,38,30,0.35)",
    orb: "#8a6d3b", buy: "#0f7a37", sell: "#b3261e",
  };
}

function chartOptions() {
  const colors = palette();
  return {
    autoSize: true,
    layout: {
      background: { type: "solid", color: colors.background },
      textColor: colors.text,
      attributionLogo: true,
      fontSize: 12,
    },
    grid: {
      vertLines: { color: colors.grid },
      horzLines: { color: colors.grid },
    },
    rightPriceScale: { borderColor: colors.border },
    timeScale: {
      borderColor: colors.border,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 2,
      barSpacing: 6,
    },
  };
}

function candleOptions() {
  const colors = palette();
  return {
    upColor: colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
    priceLineVisible: false,
  };
}

function chartAvailable() {
  return typeof window.LightweightCharts !== "undefined";
}

function initChart() {
  if (demo.chart) {
    return true;
  }
  if (!chartAvailable()) {
    const container = byId("chart");
    container.textContent =
      "The chart library did not load. Reload this page; if it keeps happening, " +
      'reinstall the dashboard files with: pip install -e ".[ui]"';
    return false;
  }
  const LightweightCharts = window.LightweightCharts;
  const colors = palette();
  demo.chart = LightweightCharts.createChart(byId("chart"), chartOptions());
  demo.series = {
    candles: demo.chart.addSeries(LightweightCharts.CandlestickSeries, candleOptions(), 0),
    emaFast: demo.chart.addSeries(LightweightCharts.LineSeries, {
      color: colors.emaFast, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, title: "EMA 9",
    }, 0),
    emaSlow: demo.chart.addSeries(LightweightCharts.LineSeries, {
      color: colors.emaSlow, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, title: "EMA 21",
    }, 0),
    rsi: demo.chart.addSeries(LightweightCharts.LineSeries, {
      color: colors.rsi, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, title: "RSI 14",
    }, 1),
    macd: demo.chart.addSeries(LightweightCharts.LineSeries, {
      color: colors.macd, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, title: "MACD 12/26/9",
    }, 2),
    signal: demo.chart.addSeries(LightweightCharts.LineSeries, {
      color: colors.signal, lineWidth: 1, priceLineVisible: false,
      lastValueVisible: false, lineStyle: LightweightCharts.LineStyle.Dashed,
    }, 2),
    hist: demo.chart.addSeries(LightweightCharts.HistogramSeries, {
      priceLineVisible: false, lastValueVisible: false,
    }, 2),
  };
  demo.markers = LightweightCharts.createSeriesMarkers(demo.series.candles, []);
  const panes = demo.chart.panes();
  if (panes.length > 1) {
    panes[1].setStretchFactor(0.45);
  }
  if (panes.length > 2) {
    panes[2].setStretchFactor(0.55);
  }
  return true;
}

function removeOrbLines() {
  if (!demo.series) {
    return;
  }
  demo.orbLines.forEach(function (line) {
    try {
      demo.series.candles.removePriceLine(line);
    } catch (error) {
      // The line belonged to a previous series instance; nothing to remove.
    }
  });
  demo.orbLines = [];
}

function applyChartTheme() {
  if (!demo.chart || !demo.series) {
    return;
  }
  const colors = palette();
  demo.chart.applyOptions({
    layout: {
      background: { type: "solid", color: colors.background },
      textColor: colors.text,
    },
    grid: {
      vertLines: { color: colors.grid },
      horzLines: { color: colors.grid },
    },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border },
  });
  demo.series.candles.applyOptions(candleOptions());
  demo.series.emaFast.applyOptions({ color: colors.emaFast });
  demo.series.emaSlow.applyOptions({ color: colors.emaSlow });
  demo.series.rsi.applyOptions({ color: colors.rsi });
  demo.series.macd.applyOptions({ color: colors.macd });
  demo.series.signal.applyOptions({ color: colors.signal });
  removeOrbLines();
  if (demo.timeline && demo.index > 0) {
    addOrbLinesIfReady(demo.timeline.candles[demo.index - 1]);
  }
  applyMarkers();
}

function updateProgress() {
  const label = byId("demo-progress");
  if (!demo.timeline) {
    label.textContent = "Loading…";
    return;
  }
  const total = demo.timeline.candles.length;
  if (demo.index === 0) {
    label.textContent = "Not started · " + total + " one-minute candles";
    return;
  }
  const candle = demo.timeline.candles[Math.min(demo.index, total) - 1];
  label.textContent = "Candle " + demo.index + " of " + total +
    (candle ? " · " + formatSeconds(candle.time) : "");
}

function addOrbLinesIfReady(candle) {
  if (demo.orbShown || !demo.timeline || !demo.timeline.orb || !demo.series) {
    return;
  }
  if (!candle || candle.time < demo.timeline.orb.formed_at) {
    return;
  }
  const colors = palette();
  const options = {
    color: colors.orb, lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
    priceLineVisible: false, title: "ORB high",
  };
  demo.orbLines.push(demo.series.candles.createPriceLine(
    Object.assign({}, options, { price: demo.timeline.orb.high }),
  ));
  demo.orbLines.push(demo.series.candles.createPriceLine(
    Object.assign({}, options, { price: demo.timeline.orb.low, title: "ORB low" }),
  ));
  demo.orbShown = true;
}

function updateSeries(candle) {
  demo.series.candles.update({
    time: candle.time, open: candle.open, high: candle.high,
    low: candle.low, close: candle.close,
  });
  const lineSeries = {
    emaFast: candle.ema_fast, emaSlow: candle.ema_slow,
    rsi: candle.rsi, macd: candle.macd, signal: candle.macd_signal,
  };
  Object.keys(lineSeries).forEach(function (key) {
    if (lineSeries[key] !== null && lineSeries[key] !== undefined) {
      demo.series[key].update({ time: candle.time, value: lineSeries[key] });
    }
  });
  if (candle.macd_hist !== null && candle.macd_hist !== undefined) {
    const colors = palette();
    demo.series.hist.update({
      time: candle.time,
      value: candle.macd_hist,
      color: candle.macd_hist >= 0 ? colors.histUp : colors.histDown,
    });
  }
  addOrbLinesIfReady(candle);
}

function ruleStatusText(status) {
  if (status === "agree") { return "agrees"; }
  if (status === "against") { return "disagrees"; }
  if (status === "neutral") { return "no opinion"; }
  return "not available";
}

function ruleMark(status) {
  if (status === "agree") { return "✓"; }
  if (status === "against") { return "✕"; }
  return "–";
}

function ruleList(rules) {
  const list = make("ul", "rule-list");
  list.setAttribute("aria-label", "Rules at this candle");
  (rules || []).forEach(function (rule) {
    const item = make("li", "rule rule-" + rule.status);
    const mark = make("span", "rule-mark", ruleMark(rule.status));
    mark.setAttribute("aria-hidden", "true");
    item.appendChild(mark);
    item.appendChild(make("span", "rule-label", rule.label));
    item.appendChild(make("span", "rule-status", ruleStatusText(rule.status)));
    list.appendChild(item);
  });
  return list;
}

function demoAlertCard(alert) {
  const side = sideClass(alert.side);
  const article = make("article", "demo-alert " + side);
  const head = make("div", "alert-head");
  head.appendChild(make("span", "badge " + side, alert.side));
  head.appendChild(make("span", "badge badge-demo", "DEMO"));
  head.appendChild(make("span", "alert-symbol", demo.timeline ? demo.timeline.symbol : ""));
  head.appendChild(make("span", "alert-time", formatSeconds(alert.time)));
  head.appendChild(make("span", "alert-price", "@ " + Number(alert.price).toFixed(2)));
  article.appendChild(head);
  article.appendChild(make("p", "alert-reason", reasonText(alert)));
  article.appendChild(ruleList(alert.rules));
  return article;
}

function applyMarkers() {
  if (!demo.markers) {
    return;
  }
  const colors = palette();
  demo.markerList = demo.revealedAlerts.map(function (alert) {
    const isBuy = alert.side === "BUY";
    return {
      time: alert.time,
      position: isBuy ? "belowBar" : "aboveBar",
      color: isBuy ? colors.buy : colors.sell,
      shape: isBuy ? "arrowUp" : "arrowDown",
      text: isBuy ? "BUY" : "SELL",
      size: 2,
    };
  });
  demo.markers.setMarkers(demo.markerList);
}

function revealAlertsAt(index) {
  if (!demo.timeline) {
    return;
  }
  demo.timeline.alerts.forEach(function (alert) {
    if (alert.index !== index) {
      return;
    }
    demo.revealedAlerts.push(alert);
    byId("demo-alerts").appendChild(demoAlertCard(alert));
    byId("demo-counts").textContent =
      demo.revealedAlerts.length + " of " + demo.timeline.alerts.length + " shown";
  });
  applyMarkers();
}

function setPlaying(playing) {
  demo.playing = playing;
  byId("demo-play").disabled = playing;
  byId("demo-pause").disabled = !playing;
  byId("demo-play").textContent = demo.done ? "Play again" : "Play";
}

function stopTimer() {
  if (demo.timer !== null) {
    window.clearInterval(demo.timer);
    demo.timer = null;
  }
}

function startTimer(speedKey) {
  stopTimer();
  const interval = SPEED_INTERVALS[speedKey] || SPEED_INTERVALS["5"];
  demo.timer = window.setInterval(tick, interval);
}

function tick() {
  if (!demo.timeline) {
    return;
  }
  const total = demo.timeline.candles.length;
  if (demo.index >= total) {
    finishDemo();
    return;
  }
  updateSeries(demo.timeline.candles[demo.index]);
  revealAlertsAt(demo.index);
  demo.index += 1;
  demo.chart.timeScale().fitContent();
  updateProgress();
  if (demo.index >= total) {
    finishDemo();
  }
}

function finishDemo() {
  stopTimer();
  demo.done = true;
  if (demo.chart) {
    demo.chart.timeScale().fitContent();
  }
  setPlaying(false);
  const counts = demo.timeline ? demo.timeline.counts : { buys: 0, sells: 0, total: 0 };
  byId("demo-summary").textContent = "Demo finished — " + counts.total +
    " alert(s): " + counts.sells + " SELL, " + counts.buys + " BUY. " +
    "Nothing was saved to your alert history.";
  updateProgress();
}

function resetDemo() {
  stopTimer();
  demo.index = 0;
  demo.done = false;
  demo.orbShown = false;
  demo.revealedAlerts = [];
  demo.markerList = [];
  clear(byId("demo-alerts"));
  byId("demo-summary").textContent = "";
  byId("demo-counts").textContent = "";
  setPlaying(false);
  updateProgress();
  if (!initChart()) {
    return;
  }
  applyChartTheme();
  demo.series.candles.setData([]);
  ["emaFast", "emaSlow", "rsi", "macd", "signal", "hist"].forEach(function (key) {
    demo.series[key].setData([]);
  });
  demo.markers.setMarkers([]);
  removeOrbLines();
  demo.chart.timeScale().fitContent();
}

function playDemo() {
  if (!demo.timeline) {
    return;
  }
  if (demo.done || demo.index >= demo.timeline.candles.length) {
    resetDemo();
  }
  if (!demo.chart && !initChart()) {
    return;
  }
  demo.chart.timeScale().fitContent();
  setPlaying(true);
  startTimer(byId("demo-speed").value);
}

function pauseDemo() {
  stopTimer();
  setPlaying(false);
}

async function loadDemo() {
  try {
    demo.timeline = await api("/api/demo/timeline");
    resetDemo();
  } catch (error) {
    byId("demo-progress").textContent = "The demo could not load: " + error.message;
  }
}

function bindDemoControls() {
  byId("demo-play").addEventListener("click", playDemo);
  byId("demo-pause").addEventListener("click", pauseDemo);
  byId("demo-reset").addEventListener("click", resetDemo);
  byId("demo-speed").addEventListener("change", function (event) {
    if (demo.playing) {
      startTimer(event.target.value);
    }
  });
}

/* ------------------------------------------------------------------ welcome */

function initWelcome() {
  const panel = byId("welcome");
  let dismissed = false;
  try {
    dismissed = window.localStorage.getItem(WELCOME_KEY) === "1";
  } catch (error) {
    dismissed = false;
  }
  show(panel, !dismissed);
  byId("welcome-dismiss").addEventListener("click", function () {
    show(panel, false);
    try {
      window.localStorage.setItem(WELCOME_KEY, "1");
    } catch (error) {
      // Private mode: the panel simply returns next time.
    }
  });
}

/* ------------------------------------------------------- test my settings */

function practiceAlert(alert) {
  const side = sideClass(alert.side);
  const article = make("article", "demo-alert " + side);
  const head = make("div", "alert-head");
  head.appendChild(make("span", "badge " + side, alert.side));
  head.appendChild(make("span", "badge badge-demo", "DEMO"));
  head.appendChild(make("span", "alert-time", formatSeconds(alert.time)));
  head.appendChild(make("span", "alert-price", "@ " + Number(alert.price).toFixed(2)));
  article.appendChild(head);
  article.appendChild(make("p", "alert-reason", reasonText(alert)));
  article.appendChild(ruleList(alert.rules));
  return article;
}

function renderPracticeCheck(data) {
  const panel = byId("practice-check-result");
  clear(panel);
  const counts = make("p", "practice-counts");
  counts.appendChild(make("span", "count-buy", data.counts.buys + " buys"));
  counts.appendChild(make("span", "count-sell", data.counts.sells + " sells"));
  panel.appendChild(counts);
  panel.appendChild(make("p", "muted", data.message));

  const list = make("div", "practice-result");
  (data.alerts || []).forEach(function (alert) {
    list.appendChild(practiceAlert(alert));
  });
  if (!(data.alerts || []).length) {
    list.appendChild(make(
      "p",
      "notice notice-quiet",
      "No alert would fire with these thresholds. That can be intentional, or a sign " +
      "that the rules are too strict.",
    ));
  }
  panel.appendChild(list);
  show(panel, true);
}

async function checkPractice() {
  const button = byId("practice-check");
  const status = byId("practice-check-status");
  button.disabled = true;
  status.className = "test-status";
  status.textContent = "Replaying the practice day with your saved settings…";
  show(byId("practice-check-result"), false);
  try {
    const data = await api("/api/practice/check", { method: "POST" });
    renderPracticeCheck(data);
    status.className = "test-status is-ok";
    status.textContent = "Replay finished — this used the settings saved on disk.";
  } catch (error) {
    status.className = "test-status is-error";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------------- init */

export function initPractice() {
  if (started) {
    return;
  }
  started = true;
  bindDemoControls();
  initWelcome();
  byId("practice-check").addEventListener("click", checkPractice);
  loadDemo();
  if (window.matchMedia) {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    if (query.addEventListener) {
      query.addEventListener("change", applyChartTheme);
    }
  }
}

export function onShowPractice() {
  if (demo.chart) {
    demo.chart.timeScale().fitContent();
  }
}
