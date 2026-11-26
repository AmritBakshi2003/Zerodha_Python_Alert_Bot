import assert from "node:assert/strict";
import test from "node:test";

import { POLL_INTERVALS, pollIntervalMs } from "../../src/kite_alerts/ui/static/polling.js";

test("delayed NSE and crypto poll; live and demo do not", () => {
  assert.equal(pollIntervalMs("nse_delayed"), 10_000);
  assert.equal(pollIntervalMs("crypto_showcase"), 2_000);
  assert.equal(pollIntervalMs("kite_live"), 0);
  assert.equal(pollIntervalMs("demo"), 0);
});

test("an unknown source never polls", () => {
  assert.equal(pollIntervalMs(""), 0);
  assert.equal(pollIntervalMs(undefined), 0);
  assert.equal(pollIntervalMs("something-new"), 0);
});

test("the schedule is the documented table", () => {
  assert.deepEqual(POLL_INTERVALS, { nse_delayed: 10_000, crypto_showcase: 2_000 });
});
