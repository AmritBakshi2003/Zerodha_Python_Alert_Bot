"""``kite-alerts`` command line.

Commands:
    run            live alerting (the production path)
    login          refresh the daily Kite access token
    replay         replay a CSV or Kite history through the same engine
    demo           offline synthetic demo (BUY + SELL, no account needed)
    ui             local read-only dashboard with an offline demo player
    config-check   validate configuration and print a redacted summary
    version        print the version
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from ..config.settings import load_settings
from ..core.errors import AuthError, ConfigError, KiteAlertsError
from ..observability.logging import setup_logging
from .run import EXIT_AUTH, EXIT_CONFIG, EXIT_ERROR, EXIT_OK, build_runtime, run_live


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kite-alerts",
        description="Zerodha Kite Connect trade-ALERT bot (alerts only; never places orders).",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    parser.add_argument("--version", action="version", version=f"kite-alerts {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the live alert bot")
    run.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop after N seconds (useful for a supervised trial run)",
    )

    sub.add_parser("login", help="refresh the daily Kite access token")

    replay = sub.add_parser("replay", help="replay data through the same engine (analysis)")
    replay.add_argument("--symbol", required=True, help="label, e.g. NSE:INFY")
    replay.add_argument("--csv", type=Path, default=None, help="CSV of 1-minute candles")
    replay.add_argument("--from-kite", action="store_true", help="use Kite history instead of CSV")
    replay.add_argument("--days", type=int, default=5, help="days of Kite history (default 5)")
    replay.add_argument("--continuous", action="store_true", help="continuous futures series")
    replay.add_argument("--json", action="store_true", help="also print machine-readable JSON")

    sub.add_parser("demo", help="offline synthetic demo (no account needed)")

    ui = sub.add_parser("ui", help="local read-only dashboard with a demo player")
    ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback address to listen on (default: 127.0.0.1)",
    )
    ui.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port to listen on (default: 8765)",
    )
    ui.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="open the dashboard in your default browser",
    )
    ui.add_argument(
        "--db",
        type=Path,
        default=None,
        help="path to the SQLite alert store (default: DB_PATH from .env)",
    )
    ui.add_argument(
        "--simulate-kite",
        action="store_true",
        help="run dashboard with synthetic Kite live feed (no Zerodha account needed)",
    )

    sub.add_parser("config-check", help="validate configuration and print a redacted summary")
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    setup_logging(
        level=settings.observability.log_level,
        log_dir=settings.observability.log_dir,
        json_logs=settings.observability.log_json,
    )
    warnings = settings.warnings_list()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    runtime = build_runtime(settings)
    for warning in warnings:
        runtime.hub.send_text(f"⚠️ config: {warning}", level="info")

    print(
        f"kite-alerts {__version__} — ALERTS ONLY, no orders. "
        f"Watching {', '.join(settings.symbols)}. Ctrl-C to stop.",
        file=sys.stderr,
    )
    return run_live(settings, runtime=runtime, duration_seconds=args.duration)


def _cmd_login(args: argparse.Namespace) -> int:
    from .login_cmd import login

    return login(env_file=args.env_file)


def _cmd_replay(args: argparse.Namespace) -> int:
    from ..backtest.replay import (
        build_report,
        build_result,
        load_csv,
        replay_frame,
        replay_kite_history,
    )
    from ..config.overrides import SymbolOverrides

    settings = load_settings(args.env_file)
    overrides = SymbolOverrides.load(settings.overrides_file, settings.params)
    params = overrides.for_symbol(args.symbol)

    if args.csv is not None:
        frame = load_csv(args.csv, symbol=args.symbol)
        alerts = replay_frame(args.symbol, frame, params)
        result = build_result(args.symbol, frame, alerts)
    elif args.from_kite:
        from .run import _kite_client

        settings.kite.require()
        kite = _kite_client(settings)
        from ..data.instruments import InstrumentResolver

        resolver = InstrumentResolver(kite, settings.cache_dir)
        instrument = resolver.resolve(args.symbol)
        result = replay_kite_history(
            kite,
            symbol=args.symbol,
            token=instrument.token,
            params=params,
            days=args.days,
            continuous=args.continuous or instrument.is_future,
        )
    else:
        print("error: pass --csv PATH or --from-kite", file=sys.stderr)
        return EXIT_CONFIG

    print(build_report(result, params=params))
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "ts": alert.ts.isoformat(),
                        "symbol": alert.symbol,
                        "side": alert.side.value,
                        "price": alert.price,
                        "reasons": list(alert.reasons),
                        "forward": result.forward.get(alert.dedup_key, {}),
                    }
                    for alert in result.alerts
                ],
                indent=2,
                default=str,
            )
        )
    return EXIT_OK


def _cmd_demo(_args: argparse.Namespace) -> int:
    from ..demo import main as demo_main

    return demo_main()


def _cmd_ui(args: argparse.Namespace) -> int:
    import os

    try:
        from ..ui.server import run_ui
    except ImportError as exc:  # fastapi/uvicorn are the optional `ui` extra
        print(
            "The dashboard needs two optional packages that are not installed.\n"
            'Install them with:  pip install -e ".[ui]"\n'
            f"(details: {exc})",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    simulate_kite = bool(
        getattr(args, "simulate_kite", False)
        or os.environ.get("SIMULATE_KITE", "").lower() in ("1", "true")
    )
    return run_ui(
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
        env_file=args.env_file,
        db_path=args.db,
        simulate_kite=simulate_kite,
    )


def _cmd_config_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    print(json.dumps(settings.redacted(), indent=2, default=str))
    warnings = settings.warnings_list()
    if warnings:
        print("\nwarnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    print("\nconfiguration is valid.", file=sys.stderr)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "run": _cmd_run,
        "login": _cmd_login,
        "replay": _cmd_replay,
        "demo": _cmd_demo,
        "ui": _cmd_ui,
        "config-check": _cmd_config_check,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        parser.error(f"unknown command {args.command}")
        return EXIT_CONFIG
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except KiteAlertsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
