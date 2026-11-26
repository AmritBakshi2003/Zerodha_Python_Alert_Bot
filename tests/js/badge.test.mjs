import assert from "node:assert/strict";
import test from "node:test";

import { FRESH_TICK_SECONDS, badgeState } from "../../src/kite_alerts/ui/static/badge.js";

const NOW = 1_800_000_000_000;

function state(overrides) {
  return badgeState(
    Object.assign(
      {
        source: "kite_live",
        simulated: false,
        feedStatus: { state: "connecting" },
        lastTickTs: null,
        now: NOW,
      },
      overrides
    )
  );
}

test("a non-live source keeps the server-set badge", () => {
  for (const source of ["nse_delayed", "crypto_showcase", "demo", ""]) {
    assert.equal(state({ source }), null);
  }
});

test("a fresh live tick renders LIVE", () => {
  const info = state({ feedStatus: { state: "live" }, lastTickTs: NOW - 5_000 });
  assert.deepEqual(info, {
    badge: "LIVE",
    key: "LIVE",
    title: "Streaming real Kite market data",
    tone: "",
  });
});

test("LIVE requires a tick inside the freshness window", () => {
  const fresh = state({ feedStatus: { state: "live" }, lastTickTs: NOW - FRESH_TICK_SECONDS * 1000 });
  assert.equal(fresh.badge, "LIVE");

  const stale = state({
    feedStatus: { state: "live", last_tick_time: "09:15:00" },
    lastTickTs: NOW - (FRESH_TICK_SECONDS * 1000 + 1000),
  });
  assert.equal(stale.badge, "Stale since 09:15:00");
  assert.equal(stale.key, "STALE");
  assert.equal(stale.tone, "is-stale");
});

test("a live feed with no tick yet is stale, never LIVE", () => {
  const info = state({ feedStatus: { state: "live" }, lastTickTs: null });
  assert.equal(info.badge, "Stale");
  assert.equal(info.key, "STALE");
});

test("every feed state maps to its documented badge", () => {
  const expected = {
    connecting: ["Connecting...", "CONNECTING", "is-waiting"],
    stale: ["Stale", "STALE", "is-stale"],
    market_closed: ["Market closed", "CLOSED", "is-waiting"],
    token_expired: ["Token expired", "EXPIRED", ""],
    no_plan: ["No plan", "NO PLAN", ""],
    disconnected: ["OFF", "OFF", ""],
    unexpected: ["OFF", "OFF", ""],
  };
  for (const [feedState, [badge, key, tone]] of Object.entries(expected)) {
    const info = state({ feedStatus: { state: feedState } });
    assert.equal(info.badge, badge, feedState);
    assert.equal(info.key, key, feedState);
    assert.equal(info.tone, tone, feedState);
  }
});

test("a stale feed carries the last tick time when the server knows it", () => {
  const info = state({ feedStatus: { state: "stale", last_tick_time: "10:31:05" } });
  assert.equal(info.badge, "Stale since 10:31:05");
});

test("a missing feed status is treated as connecting", () => {
  assert.equal(state({ feedStatus: null }).key, "CONNECTING");
});

test("SIMULATED never renders as LIVE, whatever the feed claims", () => {
  const feedStates = [
    "live",
    "connecting",
    "stale",
    "market_closed",
    "token_expired",
    "no_plan",
    "disconnected",
    "",
  ];
  const tickAges = [null, 0, 1_000, 31_000, 999_000];

  for (const feedState of feedStates) {
    for (const age of tickAges) {
      const info = state({
        simulated: true,
        feedStatus: { state: feedState },
        lastTickTs: age === null ? null : NOW - age,
      });
      assert.equal(info.badge, "SIMULATED", `${feedState}/${age}`);
      assert.equal(info.key, "SIMULATED", `${feedState}/${age}`);
      assert.notEqual(info.badge, "LIVE", `${feedState}/${age}`);
      assert.match(info.title, /SIMULATED, not real market data/);
      assert.equal(info.tone, "");
    }
  }
});

test("the caller's clock decides freshness, not the wall clock", () => {
  const feedStatus = { state: "live" };
  assert.equal(state({ feedStatus, lastTickTs: NOW - 1_000, now: NOW }).badge, "LIVE");
  assert.equal(
    state({ feedStatus, lastTickTs: NOW - 1_000, now: NOW + 60_000 }).badge,
    "Stale"
  );
});
