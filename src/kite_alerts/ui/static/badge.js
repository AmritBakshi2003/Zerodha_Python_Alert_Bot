/* Pure state machine for the chart badge.
 *
 * The badge is the only thing telling a user whether the candles on screen are
 * live Kite data, delayed data, a demo or simulated ticks. It must never say
 * LIVE unless the feed is live *and* a tick arrived inside the freshness
 * window. Keeping the mapping free of the DOM lets tests/js assert the whole
 * truth table, in particular that SIMULATED can never render as LIVE.
 */

/** A tick older than this many seconds makes a "live" feed stale. */
export const FRESH_TICK_SECONDS = 30;

/** Age used when no tick has arrived yet (the old dashboard's 999s sentinel). */
export const NO_TICK_SECONDS = 999;

function staleInfo(since) {
  return {
    badge: "Stale" + since,
    key: "STALE",
    title: "No fresh tick yet; showing the last known candles",
    tone: "is-stale",
  };
}

/**
 * Map dashboard state to what the badge should show.
 *
 * @param {object} input
 * @param {string} input.source - active data source id (`kite_live`, ...)
 * @param {boolean} [input.simulated] - the server is replaying synthetic ticks
 * @param {object|null} [input.feedStatus] - last SSE `status` payload
 * @param {number|null} [input.lastTickTs] - `Date.now()` of the last tick
 * @param {number} [input.now] - injectable clock for tests
 * @returns {{badge: string, key: string, title: string, tone: string}|null}
 *   `null` means "not the live source; leave the server badge alone".
 */
export function badgeState(input) {
  const settings = input || {};
  const source = settings.source || "";
  const now = typeof settings.now === "number" ? settings.now : Date.now();
  const feed = settings.feedStatus || {};
  const feedState = feed.state ? String(feed.state) : "connecting";
  const simulated = Boolean(settings.simulated);
  const lastTickTs = typeof settings.lastTickTs === "number" ? settings.lastTickTs : null;
  const age = lastTickTs === null ? NO_TICK_SECONDS : (now - lastTickTs) / 1000;

  if (source !== "kite_live") {
    return null;
  }
  if (simulated) {
    return {
      badge: "SIMULATED",
      key: "SIMULATED",
      title: "SIMULATED, not real market data",
      tone: "",
    };
  }

  const since = feed.last_tick_time ? " since " + feed.last_tick_time : "";

  if (feedState === "live") {
    if (age <= FRESH_TICK_SECONDS) {
      return {
        badge: "LIVE",
        key: "LIVE",
        title: "Streaming real Kite market data",
        tone: "",
      };
    }
    return staleInfo(since);
  }
  if (feedState === "connecting") {
    return {
      badge: "Connecting...",
      key: "CONNECTING",
      title: "Connecting to the Kite live ticker",
      tone: "is-waiting",
    };
  }
  if (feedState === "stale") {
    return staleInfo(since);
  }
  if (feedState === "market_closed") {
    return {
      badge: "Market closed",
      key: "CLOSED",
      title: "Live ticks resume at 09:15 IST",
      tone: "is-waiting",
    };
  }
  if (feedState === "token_expired") {
    return {
      badge: "Token expired",
      key: "EXPIRED",
      title: "Reconnect on the Connect page to use live data",
      tone: "",
    };
  }
  if (feedState === "no_plan") {
    return {
      badge: "No plan",
      key: "NO PLAN",
      title: "This Kite plan has no market data; delayed NSE is used",
      tone: "",
    };
  }
  return {
    badge: "OFF",
    key: "OFF",
    title: "Live feed is not connected",
    tone: "",
  };
}
