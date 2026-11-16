/* Small DOM helpers shared by the page modules.
 *
 * Every string from the API goes through textContent: an alert reason, a symbol
 * or a settings value can never become markup.
 */

export function byId(id) {
  return document.getElementById(id);
}

export function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

export function clear(node) {
  while (node && node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function show(node, visible) {
  if (node) {
    node.hidden = !visible;
  }
}

export async function api(path, options) {
  const settings = Object.assign({ credentials: "same-origin", cache: "no-store" }, options || {});
  const method = String(settings.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    const meta = document.querySelector('meta[name="kite-alerts-csrf"]');
    const token = meta ? meta.getAttribute("content") : "";
    if (token) {
      settings.headers = Object.assign({}, settings.headers || {}, { "X-Kite-Alerts-CSRF": token });
    }
  }
  const response = await fetch(path, settings);
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    let message = "Something went wrong (" + response.status + ").";
    if (data && (data.detail || data.message || data.label)) {
      if (typeof data.detail === "string") {
        message = data.detail;
      } else if (data.message) {
        message = data.message;
      } else {
        message = data.label;
      }
    }
    const failure = new Error(message);
    failure.status = response.status;
    failure.data = data;
    throw failure;
  }
  return data;
}

export function setStatus(node, text, tone) {
  if (!node) {
    return;
  }
  node.textContent = text || "";
  node.classList.remove("is-ok", "is-warning", "is-error");
  if (tone) {
    node.classList.add("is-" + tone);
  }
}

export function sideClass(side) {
  return String(side).toUpperCase() === "BUY" ? "buy" : "sell";
}

/* The reason text without the emoji the alert message carries: the badge already
 * shows direction, and plain text never depends on an emoji font. */
export function reasonText(item) {
  const reasons = item.reasons || [];
  return reasons.join("; ");
}

export function formatTimestamp(ts) {
  return String(ts || "").replace("T", " ").slice(0, 16);
}

export function formatSeconds(seconds) {
  const date = new Date(Number(seconds) * 1000);
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  return hours + ":" + minutes;
}

export function alertRow(item) {
  const li = make("li", "alert-item " + sideClass(item.side));
  const head = make("div", "alert-head");
  head.appendChild(make("span", "badge " + sideClass(item.side), item.side));
  head.appendChild(make("span", "alert-symbol", item.symbol));
  head.appendChild(make("span", "alert-time", item.time));
  head.appendChild(make("span", "alert-price", "@ " + Number(item.price).toFixed(2)));
  li.appendChild(head);
  li.appendChild(make("p", "alert-reason", reasonText(item)));
  return li;
}
