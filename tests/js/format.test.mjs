import assert from "node:assert/strict";
import test from "node:test";

import {
  formatPrice,
  formatSeconds,
  formatTimestamp,
  latencyText,
  reasonText,
  sideClass,
} from "../../src/kite_alerts/ui/static/format.js";

test("sideClass is case-insensitive and defaults unknown sides to sell", () => {
  assert.equal(sideClass("BUY"), "buy");
  assert.equal(sideClass("buy"), "buy");
  assert.equal(sideClass("SELL"), "sell");
  assert.equal(sideClass("short"), "sell");
  assert.equal(sideClass(undefined), "sell");
});

test("reasonText joins reasons without the direction emoji", () => {
  assert.equal(reasonText({ reasons: ["EMA cross", "RSI 72"] }), "EMA cross; RSI 72");
  assert.equal(reasonText({ reasons: [] }), "");
  assert.equal(reasonText({}), "");
  assert.equal(reasonText(null), "");
});

test("formatTimestamp keeps date and minute, never seconds", () => {
  assert.equal(formatTimestamp("2026-11-16T09:15:42+05:30"), "2026-11-16 09:15");
  assert.equal(formatTimestamp("2026-11-16 09:15"), "2026-11-16 09:15");
  assert.equal(formatTimestamp(""), "");
  assert.equal(formatTimestamp(undefined), "");
});

test("formatSeconds renders zero-padded UTC fields", () => {
  assert.equal(formatSeconds(0), "00:00");
  assert.equal(formatSeconds(3_661), "01:01");
  assert.equal(formatSeconds("5400"), "01:30");
});

test("formatPrice always has two decimals and no separators", () => {
  assert.equal(formatPrice(100), "100.00");
  assert.equal(formatPrice("100.456"), "100.46");
  assert.equal(formatPrice(1234567.5), "1234567.50");
});

test("latencyText prefers latency_ms and floors at 1 ms", () => {
  assert.equal(latencyText({ latency_ms: 12.4 }), "Latency: 12 ms");
  assert.equal(latencyText({ latency_ms: 0.2 }), "Latency: 1 ms");
  assert.equal(latencyText({ latency_ms: 0 }), "Latency: 1 ms");
});

test("latencyText explains a clock warning and falls back to the raw label", () => {
  assert.equal(
    latencyText({ latency_ms: null, latency: "PC clock may be out of sync" }),
    "Latency: PC clock may be out of sync"
  );
  assert.equal(latencyText({ latency_ms: null, latency: "42 ms" }), "Latency: 42 ms");
  assert.equal(latencyText({ latency_ms: null }), "Latency: —");
  assert.equal(latencyText(null), "Latency: —");
});
