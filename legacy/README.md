# Legacy prototype (superseded)

These modules are the original single-file prototype, kept **for reference only**.
They are not imported, packaged, tested or shipped, and they are excluded from
lint, type-checking and coverage.

The production implementation lives in `src/kite_alerts/`. Its behaviour is
identical by construction: the offline demo output is byte-compared against
`demo_output.txt` in CI, and the five voting rules are ported unchanged.

Do not run these files. Use `kite-alerts` (see the top-level README).
