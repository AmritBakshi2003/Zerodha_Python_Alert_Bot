/* Polling schedule for the non-live chart sources.
 *
 * `kite_live` streams over SSE and `demo` is a local replay, so neither polls.
 * Keeping the table here (instead of inside chart.js) means the schedule is a
 * tested contract rather than a magic number in a timer call.
 */

export const POLL_INTERVALS = {
  nse_delayed: 10_000,
  crypto_showcase: 2_000,
};

/** Milliseconds between polls for `source`; 0 means "never poll". */
export function pollIntervalMs(source) {
  return POLL_INTERVALS[source] || 0;
}
