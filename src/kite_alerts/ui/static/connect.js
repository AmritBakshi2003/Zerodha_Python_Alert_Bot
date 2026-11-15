/* Connect page: bring-your-own-key wizard.
 *
 * The password and one-time code are typed on Zerodha's own page. This module
 * only ever sees the one-time request_token that Zerodha redirects back with,
 * and never displays a saved credential.
 */

import { api, byId, clear, make, setStatus, show } from "./dom.js";

const CALLBACK_MESSAGES = {
  connected: ["ok", "Connected to Zerodha. Your daily token is saved in .env."],
  cancelled: ["warning", "The Zerodha login was cancelled. Nothing changed."],
  missing_token: ["warning", "Zerodha did not send a request token. Try Connect again."],
  no_attempt: [
    "warning",
    "That callback did not start from this dashboard, or the attempt timed out. Start again.",
  ],
  state_expired: [
    "warning",
    "The login attempt took longer than ten minutes and was cancelled. Start again.",
  ],
  bad_state: [
    "warning",
    "The login could not be verified. Start again from the Connect button.",
  ],
  exchange_failed: [
    "error",
    "Zerodha rejected that request token. It is single-use, so start again.",
  ],
  unreachable: ["error", "Zerodha could not be reached. Check your connection and try again."],
  missing_key: ["error", "Add your Kite API key and secret first."],
  missing_secret: ["error", "Your Kite API secret is missing. Add it in step 3."],
  settings: ["error", "Your settings could not be read, so the connection cannot start."],
  error: ["error", "Something went wrong while connecting. Try again."],
};

const PILLS = {
  KITE_API_KEY: "connect-key-state",
  KITE_API_SECRET: "connect-secret-state",
};

function setChip(text, on) {
  const chip = byId("connection-chip");
  const label = byId("connection-chip-text");
  if (label) {
    label.textContent = text;
  }
  if (chip) {
    chip.dataset.state = on ? "on" : "off";
  }
}

function setMessage(text, tone) {
  const message = byId("connect-message");
  message.textContent = text || "";
  let className = "notice";
  if (tone === "ok") {
    className = "notice is-ok";
  } else if (tone === "error") {
    className = "notice is-error";
  } else if (tone === "warning") {
    className = "notice is-warning";
  }
  message.className = className;
  show(message, Boolean(text));
}

function renderSession(data) {
  const label = byId("connect-status");
  const detail = byId("connect-status-detail");
  const disconnect = byId("connect-disconnect");

  if (data.status === "connected") {
    label.textContent = "Connected as " + (data.name || "your Zerodha account");
    detail.textContent = "The daily Kite token is saved; it expires about 06:00 IST.";
    show(disconnect, true);
    setChip("Kite: connected", true);
  } else if (data.status === "expired") {
    label.textContent = "Token expired — reconnect";
    detail.textContent = data.message;
    show(disconnect, Boolean(data.token_present));
    setChip("Kite: token expired", false);
  } else if (data.status === "unreachable") {
    label.textContent = "Kite is unreachable";
    detail.textContent = data.message;
    show(disconnect, Boolean(data.token_present));
    setChip("Kite: unreachable", false);
  } else {
    label.textContent = "Not connected";
    detail.textContent = data.message || "Add your own Kite API key and secret, then connect.";
    show(disconnect, false);
    setChip("Kite: not connected", false);
  }

  const redirect = data.redirect_url || "";
  if (redirect) {
    byId("connect-redirect").textContent = redirect;
    byId("help-redirect").textContent = redirect;
  }

  const fields = (data.credentials && data.credentials.fields) || [];
  fields.forEach(function (field) {
    const pill = byId(PILLS[field.key]);
    if (!pill) {
      return;
    }
    pill.dataset.state = field.is_set ? "set" : "not-set";
    pill.textContent = field.is_set ? "Set" : "Not set";
  });
}

function renderSources(data) {
  const list = byId("data-sources");
  clear(list);
  (data.sources || []).forEach(function (source) {
    const item = make("li");
    const badge = make("span", "source-badge", source.available ? "Available" : "Unavailable");
    if (source.available) {
      badge.classList.add("is-ok");
    }
    item.appendChild(badge);
    item.appendChild(make("span", "source-name", source.label));
    item.appendChild(make("span", "source-reason", source.reason));
    list.appendChild(item);
  });
  byId("data-sources-checked").textContent = data.checked_at
    ? "Checked at " + data.checked_at + " (IST)."
    : "";
}

async function loadSources(force) {
  try {
    const data = await api("/api/data-sources" + (force ? "?refresh=1" : ""));
    renderSources(data);
  } catch (error) {
    byId("data-sources-checked").textContent = error.message;
  }
}

async function loadSession() {
  try {
    const data = await api("/api/kite/session");
    renderSession(data);
  } catch (error) {
    setMessage(error.message, "error");
  }
}

async function saveCredentials() {
  const button = byId("connect-save");
  const status = byId("connect-save-status");
  const values = {};
  const key = byId("connect-key").value.trim();
  const secret = byId("connect-secret").value.trim();
  if (key) {
    values.KITE_API_KEY = key;
  }
  if (secret) {
    values.KITE_API_SECRET = secret;
  }
  if (!Object.keys(values).length) {
    setStatus(status, "Type a key or secret first; blank fields keep the saved value.", "warning");
    return;
  }
  button.disabled = true;
  setStatus(status, "Saving…", "");
  try {
    await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: values }),
    });
    byId("connect-key").value = "";
    byId("connect-secret").value = "";
    const keyPill = byId("connect-key-state");
    const secretPill = byId("connect-secret-state");
    if (values.KITE_API_KEY) {
      keyPill.dataset.state = "set";
      keyPill.textContent = "Set";
    }
    if (values.KITE_API_SECRET) {
      secretPill.dataset.state = "set";
      secretPill.textContent = "Set";
    }
    setStatus(status, "Saved. Your key and secret are in .env and will not be shown again.", "ok");
    await loadSession();
  } catch (error) {
    setStatus(status, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function startConnect() {
  const button = byId("connect-start");
  button.disabled = true;
  setMessage("Preparing Zerodha's login page…", "ok");
  try {
    const data = await api("/api/kite/login", { method: "POST" });
    setMessage(data.message, "ok");
    window.location.assign(data.start_url);
  } catch (error) {
    setMessage(error.message, "error");
    button.disabled = false;
  }
}

async function finishManual() {
  const input = byId("connect-manual-token");
  const button = byId("connect-manual-button");
  const status = byId("connect-manual-status");
  const token = input.value.trim();
  if (!token) {
    setStatus(status, "Paste the request_token value first.", "warning");
    return;
  }
  button.disabled = true;
  setStatus(status, "Exchanging the one-time token…", "");
  try {
    const data = await api("/api/kite/manual-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_token: token }),
    });
    input.value = "";
    setStatus(status, data.message, "ok");
    setMessage(data.message, "ok");
    await loadSession();
    await loadSources(true);
  } catch (error) {
    setStatus(status, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function disconnect() {
  const button = byId("connect-disconnect");
  button.disabled = true;
  try {
    const data = await api("/api/kite/disconnect", { method: "POST" });
    setMessage(data.message, "ok");
    await loadSession();
    await loadSources(true);
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function copyRedirect() {
  const text = byId("connect-redirect").textContent;
  const status = byId("connect-copy-status");
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const range = document.createRange();
      range.selectNodeContents(byId("connect-redirect"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand("copy");
      selection.removeAllRanges();
    }
    setStatus(status, "Copied. Paste it into your Kite app's Redirect URL field.", "ok");
  } catch (error) {
    setStatus(status, "Copy it by selecting the text above.", "warning");
  }
}

function showCallbackIfAny() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("connect");
  if (!code) {
    return false;
  }
  const entry = CALLBACK_MESSAGES[code] || CALLBACK_MESSAGES.error;
  setMessage(entry[1], entry[0]);
  const clean = window.location.pathname + window.location.hash;
  window.history.replaceState(null, "", clean);
  return true;
}

export function initConnect() {
  byId("connect-save").addEventListener("click", saveCredentials);
  byId("connect-start").addEventListener("click", startConnect);
  byId("connect-manual-button").addEventListener("click", finishManual);
  byId("connect-disconnect").addEventListener("click", disconnect);
  byId("connect-copy").addEventListener("click", copyRedirect);
  byId("data-sources-refresh").addEventListener("click", function () {
    loadSources(true);
  });
}

let callbackShown = false;

export async function onShowConnect() {
  const fromCallback = callbackShown ? false : showCallbackIfAny();
  callbackShown = true;
  await loadSession();
  await loadSources(fromCallback);
}
