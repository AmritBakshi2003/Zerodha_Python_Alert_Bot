/* Kite Alerts dashboard: sidebar, hash router and the read-only pages.
 *
 * Vanilla ES modules, no build step, no external requests. Every piece of text
 * from the API is written with textContent from ./dom.js.
 */

import { alertRow, api, byId, clear, formatTimestamp, make, reasonText, show } from "./dom.js";
import { initConnect, onShowConnect } from "./connect.js";
import { initPractice, onShowPractice } from "./practice.js";
import { initSettings, onShowSettings } from "./settings.js";

const ROUTES = ["dashboard", "practice", "alerts", "settings", "connect", "help"];
const TITLES = {
  dashboard: "Dashboard",
  practice: "Practice",
  alerts: "Alerts",
  settings: "Settings",
  connect: "Connect",
  help: "Help",
};

const historyState = { page: 1, pages: 1, symbol: "", side: "" };
const HISTORY_PAGE_SIZE = 25;

let dashboardTimer = null;
let practiceStarted = false;
let settingsStarted = false;
let connectStarted = false;
let aboutLoaded = false;

/* ------------------------------------------------------------------ router */

function currentRoute() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  return ROUTES.indexOf(raw) >= 0 ? raw : "dashboard";
}

function setChip(configured, connected) {
  const chip = byId("connection-chip");
  const label = byId("connection-chip-text");
  chip.dataset.state = connected ? "on" : "off";
  if (connected) {
    label.textContent = "Kite: connected";
  } else if (configured) {
    label.textContent = "Kite: token needed";
  } else {
    label.textContent = "Kite: not connected";
  }
}

function startDashboardTimer() {
  stopDashboardTimer();
  dashboardTimer = window.setInterval(function () {
    if (currentRoute() === "dashboard") {
      refreshStatus();
      refreshFeed();
    }
  }, 5000);
}

function stopDashboardTimer() {
  if (dashboardTimer !== null) {
    window.clearInterval(dashboardTimer);
    dashboardTimer = null;
  }
}

function showPage(route, focus) {
  document.querySelectorAll("[data-page]").forEach(function (section) {
    section.hidden = section.dataset.page !== route;
  });
  document.querySelectorAll(".side-nav a").forEach(function (link) {
    if (link.dataset.route === route) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });
  byId("topbar-title").textContent = TITLES[route];
  document.title = "Kite Alerts — " + TITLES[route];
  window.scrollTo(0, 0);

  if (route === "dashboard") {
    refreshStatus();
    refreshFeed();
    startDashboardTimer();
  } else {
    stopDashboardTimer();
  }

  if (route === "alerts") {
    loadHistory(historyState.page);
  }
  if (route === "help") {
    loadAbout(true);
  }
  if (route === "practice") {
    if (!practiceStarted) {
      practiceStarted = true;
      initPractice();
    }
    onShowPractice();
  }
  if (route === "settings") {
    if (!settingsStarted) {
      settingsStarted = true;
      initSettings();
    }
    onShowSettings();
  }
  if (route === "connect") {
    if (!connectStarted) {
      connectStarted = true;
      initConnect();
    }
    onShowConnect();
  }
  if (focus) {
    byId("main").focus();
  }
}

function routeChange() {
  showPage(currentRoute(), true);
}

/* --------------------------------------------------------------- dashboard */

function setStatus(running, label, detail, checked) {
  const card = byId("status-card");
  card.classList.toggle("is-running", running);
  card.classList.toggle("is-stopped", !running);
  byId("bot-status-label").textContent = label;
  byId("bot-status-detail").textContent = detail;
  byId("bot-status-checked").textContent = checked;
  byId("bot-status-dot").setAttribute("data-state", running ? "running" : "stopped");
}

function renderStatus(data) {
  const status = data.status || {};
  const running = Boolean(status.running);
  const checked = status.checked_at ? "Last checked at " + status.checked_at : "";
  setStatus(
    running,
    status.label || (running ? "Bot running" : "Bot not running"),
    status.detail || "",
    checked,
  );
  const today = data.today || {};
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
      "",
    );
  }
}

function renderFeed(data) {
  const feed = byId("alert-feed");
  clear(feed);
  const items = data.items || [];
  const empty = byId("feed-empty");
  show(empty, items.length === 0);
  if (!items.length) {
    empty.textContent = data.message || "No alerts yet. Try the Practice page.";
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

/* ------------------------------------------------------------------ history */

function sideCell(side) {
  const cell = document.createElement("td");
  cell.appendChild(make("span", "badge " + (side === "BUY" ? "buy" : "sell"), side));
  return cell;
}

function populateSymbols(symbols) {
  const select = byId("history-symbol");
  const existing = Array.prototype.map.call(select.options, function (option) {
    return option.value;
  }).slice(1);
  const same = existing.length === symbols.length && existing.every(function (value, index) {
    return value === symbols[index];
  });
  if (same) {
    return;
  }
  const selected = select.value;
  clear(select);
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All symbols";
  select.appendChild(all);
  symbols.forEach(function (symbol) {
    const option = document.createElement("option");
    option.value = symbol;
    option.textContent = symbol;
    select.appendChild(option);
  });
  select.value = symbols.indexOf(selected) >= 0 ? selected : "";
  historyState.symbol = select.value;
}

function renderHistory(data) {
  historyState.page = data.page || 1;
  historyState.pages = data.pages || 1;
  const body = byId("history-body");
  clear(body);
  const items = data.items || [];
  items.forEach(function (item) {
    const row = document.createElement("tr");
    row.appendChild(make("td", "nowrap", formatTimestamp(item.ts)));
    row.appendChild(make("td", null, item.symbol));
    row.appendChild(sideCell(item.side));
    row.appendChild(make("td", "nowrap", "@ " + Number(item.price).toFixed(2)));
    row.appendChild(make("td", "reason-cell", reasonText(item)));
    body.appendChild(row);
  });
  const empty = byId("history-empty");
  show(empty, items.length === 0);
  if (!items.length) {
    empty.textContent = data.message || "No alerts yet. Try the Practice page.";
  }
  byId("history-page").textContent =
    "Page " + historyState.page + " of " + historyState.pages + " · " +
    (data.total || 0) + " alert(s)";
  byId("history-prev").disabled = historyState.page <= 1;
  byId("history-next").disabled = historyState.page >= historyState.pages;
  populateSymbols(data.symbols || []);
}

async function loadHistory(page) {
  const params = new URLSearchParams();
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
    const empty = byId("history-empty");
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

/* --------------------------------------------------------------------- help */

async function loadAbout(force) {
  if (aboutLoaded && !force) {
    return;
  }
  try {
    const data = await api("/api/about");
    aboutLoaded = true;
    byId("about-version").textContent = data.version;
    byId("about-mode").textContent = data.mode;
    byId("about-alerts-only").textContent = data.alerts_only_statement;
    const checklist = byId("about-checklist");
    clear(checklist);
    (data.checklist || []).forEach(function (text) {
      checklist.appendChild(make("li", null, text));
    });
    const warning = byId("about-settings-warning");
    if (data.settings_error) {
      warning.textContent = "Your settings need attention: " + data.settings_error;
      show(warning, true);
    } else {
      show(warning, false);
    }
    setChip(data.kite_configured, data.token_present);
  } catch (error) {
    byId("about-version").textContent = "Could not load";
  }
}

/* --------------------------------------------------------------------- init */

function init() {
  bindHistoryControls();
  byId("status-refresh").addEventListener("click", refreshStatus);
  window.addEventListener("hashchange", routeChange);
  loadAbout(false);
  showPage(currentRoute(), false);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
