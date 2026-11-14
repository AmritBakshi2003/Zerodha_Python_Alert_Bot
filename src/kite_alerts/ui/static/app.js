/* Kite Alerts dashboard — vanilla JS, no build step, no external requests.
 *
 * Every piece of text from the API is written with textContent (never as raw
 * HTML), so an alert reason or a symbol can never become markup.
 */
(function () {
  "use strict";

  var SPEED_INTERVALS = { "1": 350, "5": 70, "20": 18, "60": 8 };
  var HISTORY_PAGE_SIZE = 25;

  var demo = {
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
    orbShown: false
  };
  var historyState = { page: 1, pages: 1, symbol: "", side: "" };
  var cooldownTimer = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function show(node, visible) {
    if (node) {
      node.hidden = !visible;
    }
  }

  async function api(path, options) {
    var settings = Object.assign({ credentials: "same-origin", cache: "no-store" }, options || {});
    var response = await fetch(path, settings);
    var data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      var message = "Something went wrong (" + response.status + ").";
      if (data && (data.detail || data.message || data.label)) {
        message = data.detail || data.message || data.label;
      }
      var failure = new Error(message);
      failure.status = response.status;
      failure.data = data;
      throw failure;
    }
    return data;
  }

  function palette() {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (dark) {
      return {
        background: "#161b22", text: "#e8edf2", grid: "#242c36", border: "#2a323c",
        up: "#43d17c", down: "#ff7b72", emaFast: "#6cb6ff", emaSlow: "#f0a35e",
        rsi: "#c792ea", macd: "#6cb6ff", signal: "#f0a35e",
        histUp: "rgba(67,209,124,0.45)", histDown: "rgba(255,123,114,0.45)",
        orb: "#c8a45c", buy: "#43d17c", sell: "#ff7b72"
      };
    }
    return {
      background: "#ffffff", text: "#17202a", grid: "#eceff2", border: "#d8dde3",
      up: "#0f7a37", down: "#b3261e", emaFast: "#1f6feb", emaSlow: "#b45309",
      rsi: "#7b3ff2", macd: "#1f6feb", signal: "#b45309",
      histUp: "rgba(15,122,55,0.35)", histDown: "rgba(179,38,30,0.35)",
      orb: "#8a6d3b", buy: "#0f7a37", sell: "#b3261e"
    };
  }

  function chartOptions() {
    var colors = palette();
    return {
      autoSize: true,
      layout: {
        background: { type: "solid", color: colors.background },
        textColor: colors.text,
        attributionLogo: true,
        fontSize: 12
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid }
      },
      rightPriceScale: { borderColor: colors.border },
      timeScale: {
        borderColor: colors.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 2,
        barSpacing: 6
      }
    };
  }

  function candleOptions() {
    var colors = palette();
    return {
      upColor: colors.up,
      downColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
      priceLineVisible: false
    };
  }

  // ---------------------------------------------------------------- dashboard
  function setStatus(running, label, detail, checked) {
    var card = byId("status-card");
    card.classList.toggle("is-running", running);
    card.classList.toggle("is-stopped", !running);
    byId("bot-status-label").textContent = label;
    byId("bot-status-detail").textContent = detail;
    byId("bot-status-checked").textContent = checked;
    byId("bot-status-dot").setAttribute("data-state", running ? "running" : "stopped");
  }

  function renderStatus(data) {
    var status = data.status || {};
    var running = Boolean(status.running);
    var checked = status.checked_at ? "Last checked at " + status.checked_at : "";
    setStatus(
      running,
      status.label || (running ? "Bot running" : "Bot not running"),
      status.detail || "",
      checked
    );
    var today = data.today || {};
    byId("count-buys").textContent = String(today.buys || 0);
    byId("count-sells").textContent = String(today.sells || 0);
    byId("counts-date").textContent = today.date ? "Date " + today.date + " (IST)" : "";
  }

  async function refreshStatus() {
    try {
      renderStatus(await api("/api/status"));
    } catch (error) {
      setStatus(
        false,
        "Dashboard not responding",
        "The dashboard's own server did not answer. Reload this page, or restart `kite-alerts ui`.",
        ""
      );
    }
  }

  function sideClass(side) {
    return String(side).toUpperCase() === "BUY" ? "buy" : "sell";
  }

  // The reason text, without the emoji that the alert message carries. The side
  // badge already conveys direction, and plain text never depends on an emoji
  // font being installed (a missing font would otherwise show an empty box).
  function reasonText(item) {
    var reasons = item.reasons || [];
    return reasons.join("; ");
  }

  function alertRow(item) {
    var li = make("li", "alert-item " + sideClass(item.side));
    var head = make("div", "alert-head");
    head.appendChild(make("span", "badge " + sideClass(item.side), item.side));
    head.appendChild(make("span", "alert-symbol", item.symbol));
    head.appendChild(make("span", "alert-time", item.time));
    head.appendChild(make("span", "alert-price", "@ " + Number(item.price).toFixed(2)));
    li.appendChild(head);
    li.appendChild(make("p", "alert-reason", reasonText(item)));
    return li;
  }

  function renderFeed(data) {
    var feed = byId("alert-feed");
    clear(feed);
    var items = data.items || [];
    var empty = byId("feed-empty");
    show(empty, items.length === 0);
    if (!items.length) {
      empty.textContent = data.message || "No alerts yet. Try the demo player.";
    }
    items.forEach(function (item) {
      feed.appendChild(alertRow(item));
    });
  }

  async function refreshFeed() {
    try {
      renderFeed(await api("/api/alerts/recent?limit=20"));
    } catch (error) {
      // The status card already reports a connectivity problem.
    }
  }

  // ------------------------------------------------------------------ history
  function formatTimestamp(ts) {
    return String(ts || "").replace("T", " ").slice(0, 16);
  }

  function sideCell(side) {
    var cell = document.createElement("td");
    cell.appendChild(make("span", "badge " + sideClass(side), side));
    return cell;
  }

  function renderHistory(data) {
    historyState.page = data.page || 1;
    historyState.pages = data.pages || 1;
    var body = byId("history-body");
    clear(body);
    var items = data.items || [];
    items.forEach(function (item) {
      var row = document.createElement("tr");
      row.appendChild(make("td", "nowrap", formatTimestamp(item.ts)));
      row.appendChild(make("td", null, item.symbol));
      row.appendChild(sideCell(item.side));
      row.appendChild(make("td", "nowrap", "@ " + Number(item.price).toFixed(2)));
      row.appendChild(make("td", "reason-cell", reasonText(item)));
      body.appendChild(row);
    });
    var empty = byId("history-empty");
    show(empty, items.length === 0);
    if (!items.length) {
      empty.textContent = data.message || "No alerts yet. Try the demo player.";
    }
    byId("history-page").textContent =
      "Page " + historyState.page + " of " + historyState.pages + " · " +
      (data.total || 0) + " alert(s)";
    byId("history-prev").disabled = historyState.page <= 1;
    byId("history-next").disabled = historyState.page >= historyState.pages;
    populateSymbols(data.symbols || []);
  }

  function populateSymbols(symbols) {
    var select = byId("history-symbol");
    var existing = Array.prototype.map.call(select.options, function (option) {
      return option.value;
    }).slice(1);
    var same = existing.length === symbols.length && existing.every(function (value, index) {
      return value === symbols[index];
    });
    if (same) {
      return;
    }
    var selected = select.value;
    clear(select);
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "All symbols";
    select.appendChild(all);
    symbols.forEach(function (symbol) {
      var option = document.createElement("option");
      option.value = symbol;
      option.textContent = symbol;
      select.appendChild(option);
    });
    select.value = symbols.indexOf(selected) >= 0 ? selected : "";
    historyState.symbol = select.value;
  }

  async function loadHistory(page) {
    var params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(HISTORY_PAGE_SIZE));
    if (historyState.symbol) {
      params.set("symbol", historyState.symbol);
    }
    if (historyState.side) {
      params.set("side", historyState.side);
    }
    try {
      renderHistory(await api("/api/alerts/history?" + params.toString()));
    } catch (error) {
      clear(byId("history-body"));
      var empty = byId("history-empty");
      show(empty, true);
      empty.textContent = error.message;
      byId("history-page").textContent = "";
      byId("history-prev").disabled = true;
      byId("history-next").disabled = true;
    }
  }

  function bindHistoryControls() {
    byId("history-symbol").addEventListener("change", function (event) {
      historyState.symbol = event.target.value;
      loadHistory(1);
    });
    byId("history-side").addEventListener("change", function (event) {
      historyState.side = event.target.value;
      loadHistory(1);
    });
    byId("history-refresh").addEventListener("click", function () {
      loadHistory(historyState.page);
    });
    byId("history-prev").addEventListener("click", function () {
      if (historyState.page > 1) {
        loadHistory(historyState.page - 1);
      }
    });
    byId("history-next").addEventListener("click", function () {
      if (historyState.page < historyState.pages) {
        loadHistory(historyState.page + 1);
      }
    });
  }

  // --------------------------------------------------------------------- demo
  function chartAvailable() {
    return typeof window.LightweightCharts !== "undefined";
  }

  function initChart() {
    if (demo.chart) {
      return true;
    }
    if (!chartAvailable()) {
      var container = byId("chart");
      container.textContent =
        "The chart library did not load. Reload this page; if it keeps happening, " +
        "reinstall the dashboard files with: pip install -e \".[ui]\"";
      return false;
    }
    var LightweightCharts = window.LightweightCharts;
    var colors = palette();
    demo.chart = LightweightCharts.createChart(byId("chart"), chartOptions());
    demo.series = {
      candles: demo.chart.addSeries(LightweightCharts.CandlestickSeries, candleOptions(), 0),
      emaFast: demo.chart.addSeries(LightweightCharts.LineSeries, {
        color: colors.emaFast, lineWidth: 2, priceLineVisible: false,
        lastValueVisible: false, title: "EMA 9"
      }, 0),
      emaSlow: demo.chart.addSeries(LightweightCharts.LineSeries, {
        color: colors.emaSlow, lineWidth: 2, priceLineVisible: false,
        lastValueVisible: false, title: "EMA 21"
      }, 0),
      rsi: demo.chart.addSeries(LightweightCharts.LineSeries, {
        color: colors.rsi, lineWidth: 2, priceLineVisible: false,
        lastValueVisible: false, title: "RSI 14"
      }, 1),
      macd: demo.chart.addSeries(LightweightCharts.LineSeries, {
        color: colors.macd, lineWidth: 2, priceLineVisible: false,
        lastValueVisible: false, title: "MACD 12/26/9"
      }, 2),
      signal: demo.chart.addSeries(LightweightCharts.LineSeries, {
        color: colors.signal, lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, lineStyle: LightweightCharts.LineStyle.Dashed
      }, 2),
      hist: demo.chart.addSeries(LightweightCharts.HistogramSeries, {
        priceLineVisible: false, lastValueVisible: false
      }, 2)
    };
    demo.markers = LightweightCharts.createSeriesMarkers(demo.series.candles, []);
    var panes = demo.chart.panes();
    if (panes.length > 1) {
      panes[1].setStretchFactor(0.45);
    }
    if (panes.length > 2) {
      panes[2].setStretchFactor(0.55);
    }
    return true;
  }

  function applyChartTheme() {
    if (!demo.chart || !demo.series) {
      return;
    }
    var colors = palette();
    demo.chart.applyOptions({
      layout: {
        background: { type: "solid", color: colors.background },
        textColor: colors.text
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid }
      },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border }
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

  function formatChartTime(seconds) {
    var date = new Date(Number(seconds) * 1000);
    var hours = String(date.getUTCHours()).padStart(2, "0");
    var minutes = String(date.getUTCMinutes()).padStart(2, "0");
    return hours + ":" + minutes;
  }

  function updateProgress() {
    var label = byId("demo-progress");
    if (!demo.timeline) {
      label.textContent = "Loading…";
      return;
    }
    var total = demo.timeline.candles.length;
    if (demo.index === 0) {
      label.textContent = "Not started · " + total + " one-minute candles";
      return;
    }
    var candle = demo.timeline.candles[Math.min(demo.index, total) - 1];
    label.textContent = "Candle " + demo.index + " of " + total +
      (candle ? " · " + formatChartTime(candle.time) : "");
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

  function addOrbLinesIfReady(candle) {
    if (demo.orbShown || !demo.timeline || !demo.timeline.orb || !demo.series) {
      return;
    }
    if (!candle || candle.time < demo.timeline.orb.formed_at) {
      return;
    }
    var colors = palette();
    var options = {
      color: colors.orb, lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
      priceLineVisible: false, title: "ORB high"
    };
    demo.orbLines.push(demo.series.candles.createPriceLine(
      Object.assign({}, options, { price: demo.timeline.orb.high })
    ));
    demo.orbLines.push(demo.series.candles.createPriceLine(
      Object.assign({}, options, { price: demo.timeline.orb.low, title: "ORB low" })
    ));
    demo.orbShown = true;
  }

  function updateSeries(candle) {
    demo.series.candles.update({
      time: candle.time, open: candle.open, high: candle.high,
      low: candle.low, close: candle.close
    });
    var lineSeries = { emaFast: candle.ema_fast, emaSlow: candle.ema_slow, rsi: candle.rsi, macd: candle.macd, signal: candle.macd_signal };
    Object.keys(lineSeries).forEach(function (key) {
      if (lineSeries[key] !== null && lineSeries[key] !== undefined) {
        demo.series[key].update({ time: candle.time, value: lineSeries[key] });
      }
    });
    if (candle.macd_hist !== null && candle.macd_hist !== undefined) {
      var colors = palette();
      demo.series.hist.update({
        time: candle.time,
        value: candle.macd_hist,
        color: candle.macd_hist >= 0 ? colors.histUp : colors.histDown
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

  function demoAlertCard(alert) {
    var side = sideClass(alert.side);
    var article = make("article", "demo-alert " + side);
    var head = make("div", "alert-head");
    head.appendChild(make("span", "badge " + side, alert.side));
    head.appendChild(make("span", "badge badge-demo", "DEMO"));
    head.appendChild(make("span", "alert-symbol", demo.timeline ? demo.timeline.symbol : ""));
    head.appendChild(make("span", "alert-time", formatChartTime(alert.time)));
    head.appendChild(make("span", "alert-price", "@ " + Number(alert.price).toFixed(2)));
    article.appendChild(head);
    article.appendChild(make("p", "alert-reason", reasonText(alert)));

    var list = make("ul", "rule-list");
    list.setAttribute("aria-label", "Rules at this candle");
    (alert.rules || []).forEach(function (rule) {
      var item = make("li", "rule rule-" + rule.status);
      var mark = make("span", "rule-mark", ruleMark(rule.status));
      mark.setAttribute("aria-hidden", "true");
      item.appendChild(mark);
      item.appendChild(make("span", "rule-label", rule.label));
      item.appendChild(make("span", "rule-status", ruleStatusText(rule.status)));
      list.appendChild(item);
    });
    article.appendChild(list);
    return article;
  }

  function applyMarkers() {
    if (!demo.markers) {
      return;
    }
    var colors = palette();
    demo.markerList = demo.revealedAlerts.map(function (alert) {
      var isBuy = alert.side === "BUY";
      return {
        time: alert.time,
        position: isBuy ? "belowBar" : "aboveBar",
        color: isBuy ? colors.buy : colors.sell,
        shape: isBuy ? "arrowUp" : "arrowDown",
        text: isBuy ? "BUY" : "SELL",
        size: 2
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
    var interval = SPEED_INTERVALS[speedKey] || SPEED_INTERVALS["5"];
    demo.timer = window.setInterval(tick, interval);
  }

  function tick() {
    if (!demo.timeline) {
      return;
    }
    var total = demo.timeline.candles.length;
    if (demo.index >= total) {
      finishDemo();
      return;
    }
    updateSeries(demo.timeline.candles[demo.index]);
    revealAlertsAt(demo.index);
    demo.index += 1;
    // Keep the whole day filling the panel as it grows instead of pinning the
    // newest candle to the right edge and leaving blank history before 09:15.
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
    var counts = demo.timeline ? demo.timeline.counts : { buys: 0, sells: 0, total: 0 };
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

  async function loadDemo() {
    try {
      demo.timeline = await api("/api/demo/timeline");
      resetDemo();
    } catch (error) {
      byId("demo-progress").textContent = "The demo could not load: " + error.message;
    }
  }

  // ---------------------------------------------------------------- test alert
  function setTestStatus(text, tone) {
    var status = byId("test-alert-status");
    status.textContent = text;
    status.classList.remove("is-ok", "is-warning", "is-error");
    if (tone) {
      status.classList.add("is-" + tone);
    }
  }

  function renderTestAlert(data) {
    setTestStatus(data.label + " — " + data.detail, data.ok ? "ok" : "warning");
    var results = byId("test-alert-results");
    clear(results);
    (data.results || []).forEach(function (result) {
      var item = make("li", result.ok ? "result-ok" : "result-fail");
      var mark = make("span", "result-mark", result.ok ? "✓" : "✕");
      mark.setAttribute("aria-hidden", "true");
      item.appendChild(mark);
      item.appendChild(make("span", null, result.message));
      results.appendChild(item);
    });
    var setup = byId("test-alert-setup");
    clear(setup);
    (data.setup || []).forEach(function (hint) {
      setup.appendChild(make("li", null, hint));
    });
  }

  function startCooldown(seconds) {
    var button = byId("test-alert-button");
    var remaining = Math.max(1, Math.ceil(Number(seconds) || 10));
    button.disabled = true;
    if (cooldownTimer !== null) {
      window.clearTimeout(cooldownTimer);
    }
    var step = function () {
      if (remaining <= 0) {
        button.disabled = false;
        setTestStatus("You can send another test now.", "");
        cooldownTimer = null;
        return;
      }
      setTestStatus(
        "Please wait " + remaining + " more second(s) before sending another test.",
        "warning"
      );
      remaining -= 1;
      cooldownTimer = window.setTimeout(step, 1000);
    };
    step();
  }

  async function sendTestAlert() {
    var button = byId("test-alert-button");
    button.disabled = true;
    setTestStatus("Sending the test message…", "");
    clear(byId("test-alert-results"));
    clear(byId("test-alert-setup"));
    try {
      renderTestAlert(await api("/api/test-alert", { method: "POST" }));
      button.disabled = false;
    } catch (error) {
      if (error.status === 429 && error.data) {
        renderTestAlert(error.data);
        startCooldown(error.data.retry_after);
      } else {
        setTestStatus(error.message, "error");
        button.disabled = false;
      }
    }
  }

  function bindTestAlert() {
    byId("test-alert-button").addEventListener("click", sendTestAlert);
  }

  // --------------------------------------------------------------------- help
  async function loadAbout() {
    try {
      var data = await api("/api/about");
      byId("about-version").textContent = data.version;
      byId("about-mode").textContent = data.mode;
      byId("about-alerts-only").textContent = data.alerts_only_statement;
      var checklist = byId("about-checklist");
      clear(checklist);
      (data.checklist || []).forEach(function (text) {
        checklist.appendChild(make("li", null, text));
      });
      var warning = byId("about-settings-warning");
      if (data.settings_error) {
        warning.textContent = "Your settings need attention: " + data.settings_error;
        show(warning, true);
      } else {
        show(warning, false);
      }
    } catch (error) {
      byId("about-version").textContent = "Could not load";
    }
  }

  function init() {
    bindDemoControls();
    bindHistoryControls();
    bindTestAlert();
    byId("status-refresh").addEventListener("click", refreshStatus);

    loadAbout();
    refreshStatus();
    refreshFeed();
    loadHistory(1);
    loadDemo();

    window.setInterval(refreshStatus, 5000);
    window.setInterval(refreshFeed, 5000);

    if (window.matchMedia) {
      var query = window.matchMedia("(prefers-color-scheme: dark)");
      if (query.addEventListener) {
        query.addEventListener("change", applyChartTheme);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
