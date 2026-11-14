# Vendored: TradingView Lightweight Charts™

This directory contains the chart library used by the dashboard. It is committed
to the repository on purpose: the dashboard must work with no internet
connection, so there is no CDN and no runtime download.

| File | Source |
|---|---|
| `lightweight-charts.standalone.production.js` | `lightweight-charts@5.0.9`, `dist/lightweight-charts.standalone.production.js` |
| `LICENSE` | `lightweight-charts@5.0.9`, `LICENSE` |
| `NOTICE` | `lightweight-charts@5.0.9`, `NOTICE` (from the project's Git tag `v5.0.9`) |

- Upstream: <https://github.com/tradingview/lightweight-charts>
- npm tarball: <https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.0.9.tgz>
- SHA-256 of the vendored JS:
  `e6bd6516d9ab9cb897c0d6438e3fe1126f2fdce07e68c23836e84d527de81f52`

The file is unmodified. Upgrading means replacing all three files, updating the
version and hash above, and re-running the dashboard tests.

## Licence obligations (verified from the package itself)

The package declares `"license": "Apache-2.0"`. The `NOTICE` file contains:

```
TradingView Lightweight Charts™
Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/
```

The upstream `README.md` (v5.0.9) states, in the "License" section:

> This license requires specifying TradingView as the product creator.
> You shall add the "attribution notice" from the NOTICE file and a link to
> <https://www.tradingview.com/> to the page of your website or mobile application
> that is available to your users.

and notes that the chart's `attributionLogo` layout option displays a suitable
link on the chart itself.

Requirements therefore met by this repository:

1. the full Apache License 2.0 text ships in `LICENSE` and is served at
   `/license`;
2. the `NOTICE` text ships in `NOTICE`, is served at `/notice`, and is shown in
   the dashboard footer together with a link to <https://www.tradingview.com/>;
3. the chart is created with `layout.attributionLogo: true` (the upstream logo
   link on the chart);
4. the JS file is unmodified, and its upstream copyright header is retained;
5. the chart injects one 205-byte `<style>` element for the attribution logo.
   The dashboard allows exactly that stylesheet by SHA-256 hash
   (`CHART_STYLE_SHA256` in `ui/security.py`) instead of enabling
   `'unsafe-inline'`. Recompute the hash after upgrading:
   `sha256sum` the `<style>` text content (it is the single style tag created by
   the library, not the licence file).

It also incorporates parts of `tslib`, covered by the BSD Zero Clause License
(stated in the upstream README); no separate action is required for it.
