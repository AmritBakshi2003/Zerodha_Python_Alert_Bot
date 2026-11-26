/* Pure formatting helpers shared by dom.js and chart.js.
 *
 * Nothing here touches the DOM, so tests/js can import the module under Node.
 * Every value that reaches the page still goes through textContent at the call
 * site: these functions only build strings.
 */

/** CSS class for an alert side. Anything that is not BUY reads as sell. */
export function sideClass(side) {
  return String(side).toUpperCase() === "BUY" ? "buy" : "sell";
}

/* The reason text without the emoji the alert message carries: the badge already
 * shows direction, and plain text never depends on an emoji font. */
export function reasonText(item) {
  const reasons = (item && item.reasons) || [];
  return reasons.join("; ");
}

/** `2026-11-16T09:15:00+05:30` -> `2026-11-16 09:15` (the audit-log format). */
export function formatTimestamp(ts) {
  return String(ts || "").replace("T", " ").slice(0, 16);
}

/** Seconds since the epoch -> `HH:MM` in UTC fields. */
export function formatSeconds(seconds) {
  const date = new Date(Number(seconds) * 1000);
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  return hours + ":" + minutes;
}

/** Price for the alert row; always two decimals, never a locale separator. */
export function formatPrice(price) {
  return Number(price).toFixed(2);
}

/**
 * One-line latency label for the live feed bar.
 *
 * `latency_ms` wins; a huge value is reported as a clock warning because the
 * age of a tick can be skewed by the PC clock, not by Kite.
 */
export function latencyText(data) {
  const payload = data || {};
  if (payload.latency_ms !== null && payload.latency_ms !== undefined) {
    const ms = Math.max(1, Math.round(payload.latency_ms));
    return "Latency: " + ms + " ms";
  }
  if (payload.latency && payload.latency.indexOf("sync") >= 0) {
    return "Latency: PC clock may be out of sync";
  }
  return "Latency: " + (payload.latency || "—");
}
