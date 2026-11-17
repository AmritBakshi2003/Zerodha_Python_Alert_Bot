"""Read-only chart orchestration: freshness, indicators, signal meter, watchlist.

Every number the page draws is computed here with the bot's own indicator
functions. This module never imports the engine, the notifier or the alert
store, so no chart request can create an alert.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ...config.defaults import RULE_IDS
from ...config.settings import Settings, StrategyParams
from ...core.calendar import TradingCalendar, calendar_from_config
from ...core.clock import IST, SystemClock, to_ist
from ...indicators import ema, macd, opening_range, rsi
from ...strategy.evaluator import evaluate
from .sources import INTERVALS, ChartSource, default_sources
from .symbols import is_safe_symbol, resolve_nse_symbol
from .transforms import (
    age_text,
    change_percent,
    heikin_ashi,
    ist_label,
    ist_time_label,
    to_frame,
)
from .types import ChartError, SourceError, SourceInfo, SourceSeries, SymbolInfo

RULE_LABELS: dict[str, str] = {
    "ema_cross": "EMA",
    "rsi_zone": "RSI",
    "macd": "MACD",
    "orb": "ORB",
    "oi": "OI",
}

CHART_TYPES: tuple[str, ...] = (
    "candles",
    "hollow",
    "heikin",
    "bars",
    "line",
    "area",
    "baseline",
)

MAX_CANDLES = 600
DEFAULT_CANDLES = 300
WATCHLIST_LIMIT = 10


def indicator_frame(frame: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """Indicator columns, using exactly the functions the engine's rules use."""
    result = pd.DataFrame(index=frame.index)
    result["ema_fast"] = ema(frame["close"], params.ema_fast, seed=params.indicator_seed)
    result["ema_slow"] = ema(frame["close"], params.ema_slow, seed=params.indicator_seed)
    result["rsi"] = rsi(frame["close"], params.rsi_period, seed=params.indicator_seed)
    line, signal, histogram = macd(
        frame["close"],
        params.macd_fast,
        params.macd_slow,
        params.macd_signal,
        seed=params.indicator_seed,
    )
    result["macd"] = line
    result["macd_signal"] = signal
    result["macd_hist"] = histogram
    return result


def _safe(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class SignalMeter:
    summary: str
    side: str
    agree: int
    rules: tuple[dict[str, str], ...]
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "side": self.side,
            "agree": self.agree,
            "rules": [dict(rule) for rule in self.rules],
            "label": self.label,
        }


def build_signal(
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    source_id: str,
    source_label: str,
    session_based: bool,
    supports_oi: bool,
) -> SignalMeter:
    """A read-only interpretation of the five configured rules."""
    evaluation = evaluate("CHART", frame, params, detail=True)
    votes = {vote.rule_id: vote for vote in evaluation.votes}
    rows: list[dict[str, str]] = []
    for rule_id in RULE_IDS:
        label = RULE_LABELS[rule_id]
        rule_settings = params.rules.get(rule_id)
        rule_enabled = bool(rule_settings.enabled) if rule_settings is not None else False
        if not rule_enabled:
            rows.append(
                {"id": rule_id, "label": label, "status": "unavailable", "state": "Off in settings"}
            )
            continue
        if rule_id == "orb" and not session_based:
            rows.append(
                {
                    "id": rule_id,
                    "label": label,
                    "status": "unavailable",
                    "state": "No NSE session on this source",
                }
            )
            continue
        if rule_id == "oi" and not supports_oi:
            rows.append(
                {
                    "id": rule_id,
                    "label": label,
                    "status": "unavailable",
                    "state": "No OI data from this source",
                }
            )
            continue
        vote = votes.get(rule_id)
        if vote is None:
            rows.append(
                {
                    "id": rule_id,
                    "label": label,
                    "status": "unavailable",
                    "state": "Not enough history yet",
                }
            )
            continue
        status = "bullish" if vote.vote > 0 else "bearish" if vote.vote < 0 else "neutral"
        state = (
            vote.reason
            or {
                "bullish": "Points up",
                "bearish": "Points down",
                "neutral": "No clear signal",
            }[status]
        )
        rows.append({"id": rule_id, "label": label, "status": status, "state": state})

    bullish = sum(1 for row in rows if row["status"] == "bullish")
    bearish = sum(1 for row in rows if row["status"] == "bearish")
    if bullish > bearish:
        side = "bullish"
        agree = bullish
    elif bearish > bullish:
        side = "bearish"
        agree = bearish
    else:
        side = "mixed"
        agree = max(bullish, bearish)

    if side == "mixed" and agree:
        summary = f"{agree} of 5 agree, the rest are split (no clear side yet)"
    elif side == "mixed":
        summary = "No rule has a clear view yet"
    else:
        waiting = next(
            (row["label"] for row in rows if row["status"] != side),
            "",
        )
        summary = f"{agree} of 5 agree ({side})"
        if waiting:
            summary += f", waiting for {waiting}"
    label = f"Informational, based on {source_label} data. No alerts are sent from this source."
    return SignalMeter(summary=summary, side=side, agree=agree, rules=tuple(rows), label=label)


@dataclass
class ChartService:
    settings_provider: Callable[[], Settings | None]
    sources: dict[str, ChartSource] = field(default_factory=default_sources)
    watchlist_path: Path = Path("data/chart_watchlist.json")
    clock: SystemClock = field(default_factory=SystemClock)
    rate_limit_per_minute: int = 300
    _calls: deque[float] = field(default_factory=deque, init=False)
    _watchlist_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _watch_cache: tuple[str, float, dict[str, Any]] | None = field(default=None, init=False)
    _calendar_cache: tuple[int, TradingCalendar] | None = field(default=None, init=False)

    # --------------------------------------------------------------- validation
    def source(self, source_id: str) -> ChartSource:
        source = self.sources.get(str(source_id))
        if source is None:
            raise ChartError(
                "Unknown data source. Choose Delayed NSE, Crypto showcase, Demo replay "
                "or Kite live.",
                status=422,
                code="unknown_source",
            )
        return source

    def source_info(self, source_id: str) -> SourceInfo:
        source = self.source(source_id)
        notice = {
            "nse_delayed": "Unofficial delayed NSE data. Not live.",
            "crypto_showcase": "Crypto showcase is not NSE data.",
            "demo": "A made-up day for practice. Nothing is saved.",
        }.get(source.id, "")
        if source.id == "kite_live":
            # The source owns the wording so it can never drift from the feed.
            notice = str(getattr(source, "notice", "") or notice)
        enabled = source.enabled
        return SourceInfo(
            id=source.id,
            label=source.label,
            badge=source.badge,
            available=enabled,
            enabled=enabled,
            reason="" if enabled else notice,
            notice=notice,
            supports_oi=source.supports_oi,
        )

    def validate_interval(self, interval: int) -> int:
        try:
            value = int(interval)
        except (TypeError, ValueError):
            raise ChartError("Choose a timeframe of 1, 3, 5 or 15 minutes.") from None
        if value not in INTERVALS:
            raise ChartError("Choose a timeframe of 1, 3, 5 or 15 minutes.")
        return value

    def validate_limit(self, limit: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            raise ChartError("Candle limit must be a number.") from None
        if value < 10 or value > MAX_CANDLES:
            raise ChartError(f"Candle limit must be between 10 and {MAX_CANDLES}.")
        return value

    def _symbols_for(
        self, source: ChartSource, settings: Settings | None
    ) -> tuple[SymbolInfo, ...]:
        symbols = list(source.symbols(settings))
        if source.id == "nse_delayed":
            known = {info.symbol for info in symbols}
            for extra in self._read_extras().get("nse_delayed", []):
                if extra in known:
                    continue
                known.add(extra)
                symbols.append(resolve_nse_symbol(extra))
        return tuple(symbols)

    def resolve_symbol(
        self, source: ChartSource, symbol: str, settings: Settings | None
    ) -> SymbolInfo:
        requested = str(symbol or "").strip().upper()
        if not requested or not is_safe_symbol(requested):
            raise ChartError(
                "Choose a symbol from the list. Symbols may contain letters, numbers and : . & _ -",
                code="invalid_symbol",
            )
        if source.id == "nse_delayed":
            configured = settings.symbols if settings is not None else ()
            from .symbols import lookup_nse

            info = lookup_nse(configured, requested)
            if not info.supported:
                raise ChartError(info.reason, code="unsupported_symbol")
            return info
        for info in source.symbols(settings):
            if info.symbol == requested:
                return info
        raise ChartError(
            f"{requested} is not available from {source.label}. Choose one of the listed symbols.",
            code="invalid_symbol",
        )

    # ------------------------------------------------------------------- series
    def _series(
        self, source: ChartSource, symbol: str, interval: int, settings: Settings | None
    ) -> tuple[SymbolInfo, SourceSeries]:
        info = self.resolve_symbol(source, symbol, settings)
        try:
            series = source.fetch(info.symbol, interval, settings)
        except SourceError as exc:
            raise ChartError(exc.message, status=503, code=exc.code) from None
        if not series.candles:
            raise ChartError(f"No candles are available from {source.label} right now.", status=503)
        return info, series

    def _params(self, settings: Settings | None) -> StrategyParams:
        return settings.params if settings is not None else StrategyParams()

    def _calendar(self, settings: Settings | None) -> TradingCalendar:
        key = id(settings)
        if self._calendar_cache is not None and self._calendar_cache[0] == key:
            return self._calendar_cache[1]
        if settings is None:
            calendar = TradingCalendar()
        else:
            calendar = calendar_from_config(
                open_value=settings.session.open_time,
                close_value=settings.session.close_time,
                holiday_file=settings.session.holiday_file,
                special_sessions_file=settings.session.special_sessions_file,
                pre_open_value=settings.session.pre_open_time,
                closing_value=settings.session.closing_end,
            )
        self._calendar_cache = (key, calendar)
        return calendar

    def candles(
        self,
        source_id: str,
        symbol: str,
        interval: int = 5,
        limit: int = DEFAULT_CANDLES,
    ) -> dict[str, Any]:
        source = self.source(source_id)
        if not source.enabled:
            disabled = self.source_info(source.id)
            reason = disabled.reason or disabled.notice
            detail = f"{disabled.label} is not available right now."
            if reason:
                detail = f"{detail} {reason}"
            raise ChartError(
                f"{detail} Choose Delayed NSE, Crypto showcase or Demo replay.",
                status=422,
                code="source_disabled",
            )
        interval = self.validate_interval(interval)
        limit = self.validate_limit(limit)
        settings = self.settings_provider()
        info, series = self._series(source, symbol, interval, settings)
        params = self._params(settings)

        frame = to_frame(series.candles)
        indicators = indicator_frame(frame, params)
        combined = pd.concat([frame, indicators], axis=1)
        visible = combined.tail(limit).copy()
        ha_values = {
            int(row["time"]): row
            for row in heikin_ashi(series.candles[-len(visible) :] if len(visible) else ())
        }

        candles: list[dict[str, Any]] = []
        for ts, row in visible.iterrows():
            epoch = int(ts.timestamp())
            ha = ha_values.get(epoch, {})
            candles.append(
                {
                    "time": epoch,
                    "ist": ist_label(epoch),
                    "open": _safe(row["open"]),
                    "high": _safe(row["high"]),
                    "low": _safe(row["low"]),
                    "close": _safe(row["close"]),
                    "volume": _safe(row["volume"]),
                    "ema_fast": _safe(row["ema_fast"]),
                    "ema_slow": _safe(row["ema_slow"]),
                    "rsi": _safe(row["rsi"]),
                    "macd": _safe(row["macd"]),
                    "macd_signal": _safe(row["macd_signal"]),
                    "macd_hist": _safe(row["macd_hist"]),
                    "oi": _safe(row["oi"]) if "oi" in combined.columns else None,
                    "ha_open": _safe(ha.get("open")),
                    "ha_high": _safe(ha.get("high")),
                    "ha_low": _safe(ha.get("low")),
                    "ha_close": _safe(ha.get("close")),
                }
            )

        now = self.clock.now()
        last_epoch = series.candles[-1].time
        age_seconds = max(0.0, now.timestamp() - last_epoch)
        market = self.market_clock(now)
        session_based = source.session_based
        supports_oi = bool(series.supports_oi and "oi" in combined.columns)
        orb = None
        if session_based:
            opening = opening_range(
                frame,
                params.orb_minutes,
                session_open=self._calendar(settings).window.open_time,
            )
            if opening is not None:
                orb = {
                    "high": opening.high,
                    "low": opening.low,
                    "start": int(opening.start.timestamp()),
                    "formed_at": int(opening.end.timestamp()),
                    "label": (
                        f"Opening range {opening.start.strftime('%H:%M')}–"
                        f"{opening.end.strftime('%H:%M')}"
                    ),
                }

        signal_frame = combined.tail(max(200, params.max_bars)).copy()
        signal_supports_oi = supports_oi
        if len(signal_frame) < params.warmup_bars and interval != 1:
            # A 15-minute (or short Demo) view can be younger than the indicator
            # warm-up. Read the native 1-minute history for the meter only, so
            # the chart timeframe never changes the meaning of the five rules.
            try:
                native = source.fetch(info.symbol, 1, settings)
                native_frame = to_frame(native.candles)
                signal_frame = pd.concat(
                    [native_frame, indicator_frame(native_frame, params)], axis=1
                ).tail(max(200, params.max_bars))
                signal_supports_oi = bool(native.supports_oi and "oi" in signal_frame.columns)
            except SourceError:
                pass
        if "oi" not in signal_frame.columns:
            signal_frame["oi"] = float("nan")
        signal = build_signal(
            signal_frame,
            params,
            source_id=source.id,
            source_label=source.label,
            session_based=session_based,
            supports_oi=signal_supports_oi,
        )

        message = series.message
        if not message and source.session_based and not market["is_open"]:
            message = f"NSE is closed ({market['state']}). Showing the latest available candles."
        return {
            "ok": True,
            "source": source.id,
            "source_label": source.label,
            "badge": source.badge,
            "symbol": info.symbol,
            "symbol_label": info.label,
            "interval": interval,
            "provider_interval": series.provider_interval,
            "candles": candles,
            "chart_types": list(CHART_TYPES),
            "orb": orb,
            "oi": {
                "available": supports_oi,
                "message": "" if supports_oi else "No OI data from this source",
            },
            "signal": signal.as_dict(),
            "freshness": {
                "latest_ist": ist_label(last_epoch),
                "latest_time": ist_time_label(last_epoch),
                "age_seconds": round(age_seconds, 1),
                "age_text": age_text(age_seconds),
                "delay_seconds": series.delay_seconds,
                "checked_at": now.strftime("%H:%M:%S"),
                "stale": series.stale,
                "stale_since": (
                    dt.datetime.fromtimestamp(series.stale_since, tz=IST).strftime("%H:%M:%S")
                    if series.stale_since
                    else ""
                ),
            },
            "market": market,
            "stale": series.stale,
            "retry_after": round(series.retry_after, 1),
            "message": message,
            "notice": self.source_info(source.id).notice,
            # The feed state this payload was rendered from, "" for non-feed
            # sources (N1: the browser can never claim LIVE from stale text).
            "feed_state": getattr(source, "availability_state", ""),
        }

    def live(self, source_id: str, symbol: str, interval: int = 1) -> dict[str, Any]:
        payload = self.candles(source_id, symbol, interval=interval, limit=10)
        payload["candles"] = payload["candles"][-2:]
        return payload

    # ------------------------------------------------------------------- routes
    def symbols_payload(self, source_id: str) -> dict[str, Any]:
        source = self.source(source_id)
        settings = self.settings_provider()
        return {
            "ok": True,
            "source": source.id,
            "label": source.label,
            "badge": source.badge,
            "enabled": source.enabled,
            "supports_oi": source.supports_oi,
            "interval": 5,
            "intervals": list(INTERVALS),
            "chart_types": list(CHART_TYPES),
            "symbols": [info.as_dict() for info in self._symbols_for(source, settings)],
            "notice": self.source_info(source.id).notice,
            "feed_state": getattr(source, "availability_state", ""),
        }

    def market_clock(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """The dashboard clock, rendered entirely from ``session_info`` (F4)."""
        settings = self.settings_provider()
        calendar = self._calendar(settings)
        moment = to_ist(now or self.clock.now())
        info = calendar.session_info(moment)

        if info.tradable and info.closes_at:
            target = pd.Timestamp(info.closes_at)
            event = "Closes"
        else:
            target = pd.Timestamp(info.opens_at) if info.opens_at else calendar.next_open(moment)
            event = "Opens"
        seconds = max(0, int((target - moment).total_seconds()))

        payload = info.as_dict()
        payload.update(
            {
                "notice": "" if info.tradable else info.detail,
                "next_event": event,
                "next_event_at": target.strftime("%Y-%m-%d %H:%M"),
                "countdown_seconds": seconds,
                "holiday": calendar.is_holiday(moment.date()),
            }
        )
        return payload

    def holiday_summary(self, today: dt.date | None = None) -> dict[str, Any]:
        """What /api/about shows about the configured calendar (F3)."""
        calendar = self._calendar(self.settings_provider())
        day = today or to_ist(self.clock.now()).date()
        return {
            "count": len(calendar.holiday_dates),
            "source": calendar.source,
            "year": day.year,
            "dates": sorted(d.isoformat() for d in calendar.holiday_dates if d.year == day.year),
            "warnings": calendar.holiday_warnings(day),
            "special_sessions": [
                {
                    "date": session.day.isoformat(),
                    "open": session.open_time.strftime("%H:%M"),
                    "close": session.close_time.strftime("%H:%M"),
                    "label": session.label,
                }
                for session in calendar.configured_special_sessions
            ],
        }

    def clock_payload(self) -> dict[str, Any]:
        return {"ok": True, "market": self.market_clock()}

    # ---------------------------------------------------------------- watchlist
    def _read_extras(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self.watchlist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        symbols = raw.get("symbols") if isinstance(raw, dict) else None
        if not isinstance(symbols, dict):
            return {}
        result: dict[str, list[str]] = {}
        for source_id, values in symbols.items():
            if source_id not in self.sources or not isinstance(values, list):
                continue
            result[source_id] = [
                str(value).strip().upper()
                for value in values
                if isinstance(value, str) and is_safe_symbol(value)
            ][:WATCHLIST_LIMIT]
        return result

    def _write_extras(self, extras: dict[str, list[str]]) -> None:
        path = self.watchlist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A per-writer name: two concurrent saves must never write the same temp
        # file (on Windows that is a PermissionError, elsewhere a torn read).
        temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(
                json.dumps({"symbols": extras}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temp.replace(path)
        finally:
            # A successful replace already removed the temp; on failure do not
            # leave a stray file behind.
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)

    def add_watchlist(self, source_id: str, symbol: str) -> dict[str, Any]:
        source = self.source(source_id)
        if source.id != "nse_delayed":
            raise ChartError(
                "Only the delayed NSE watchlist accepts added symbols; the other sources "
                "have a fixed short list.",
                code="fixed_watchlist",
            )
        info = resolve_nse_symbol(symbol)
        if not info.supported:
            raise ChartError(info.reason, code="unsupported_symbol")
        with self._watchlist_lock:
            extras = self._read_extras()
            current = extras.setdefault("nse_delayed", [])
            if info.symbol not in current:
                if len(current) >= WATCHLIST_LIMIT:
                    raise ChartError(
                        f"The watchlist already has {WATCHLIST_LIMIT} added symbols. "
                        "Remove one first."
                    )
                current.append(info.symbol)
                self._write_extras(extras)
            self._watch_cache = None
        return self.watchlist(source_id)

    def remove_watchlist(self, source_id: str, symbol: str) -> dict[str, Any]:
        source = self.source(source_id)
        if source.id != "nse_delayed":
            raise ChartError("This source has a fixed watchlist.", code="fixed_watchlist")
        normalized = str(symbol or "").strip().upper()
        with self._watchlist_lock:
            extras = self._read_extras()
            current = extras.get("nse_delayed", [])
            if normalized not in current:
                raise ChartError(
                    "That symbol is not a user-added watchlist entry. Symbols from Settings "
                    "cannot be removed here.",
                    code="not_removable",
                )
            extras["nse_delayed"] = [value for value in current if value != normalized]
            self._write_extras(extras)
            self._watch_cache = None
        return self.watchlist(source_id)

    def watchlist(self, source_id: str) -> dict[str, Any]:
        source = self.source(source_id)
        settings = self.settings_provider()
        infos = self._symbols_for(source, settings)
        key = source.id + ":" + ",".join(info.symbol for info in infos)
        now_monotonic = time.monotonic()
        if (
            self._watch_cache is not None
            and self._watch_cache[0] == key
            and now_monotonic - self._watch_cache[1] < 15.0
        ):
            return self._watch_cache[2]

        added = set(self._read_extras().get("nse_delayed", []))
        items: list[dict[str, Any]] = []
        for info in infos[:WATCHLIST_LIMIT]:
            if not info.supported:
                items.append(
                    {
                        "symbol": info.symbol,
                        "label": info.label,
                        "supported": False,
                        "removable": False,
                        "reason": info.reason,
                    }
                )
                continue
            try:
                series = source.fetch(info.symbol, 5, settings)
            except SourceError as exc:
                items.append(
                    {
                        "symbol": info.symbol,
                        "label": info.label,
                        "supported": True,
                        "removable": info.symbol in added,
                        "reason": exc.message,
                    }
                )
                continue
            if not series.candles:
                items.append(
                    {
                        "symbol": info.symbol,
                        "label": info.label,
                        "supported": True,
                        "removable": info.symbol in added,
                        "reason": "No candles returned.",
                    }
                )
                continue
            closes = [candle.close for candle in series.candles[-30:]]
            items.append(
                {
                    "symbol": info.symbol,
                    "label": info.label,
                    "supported": True,
                    "removable": info.symbol in added,
                    "last": series.candles[-1].close,
                    "change_pct": change_percent(series.candles),
                    "sparkline": closes,
                    "stale": series.stale,
                    "age_text": age_text(
                        max(0.0, self.clock.now().timestamp() - series.candles[-1].time)
                    ),
                    "reason": series.message,
                }
            )
        payload = {
            "ok": True,
            "source": source.id,
            "badge": source.badge,
            "items": items,
            "checked_at": self.clock.now().strftime("%H:%M:%S"),
        }
        self._watch_cache = (key, now_monotonic, payload)
        return payload

    # -------------------------------------------------------------------- limits
    def allow_request(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60.0:
            self._calls.popleft()
        if len(self._calls) >= self.rate_limit_per_minute:
            return False
        self._calls.append(now)
        return True

    def clear_caches(self) -> None:
        for source in self.sources.values():
            cache = getattr(source, "cache", None)
            if cache is not None:
                cache.clear()
        self._watch_cache = None


__all__ = [
    "CHART_TYPES",
    "DEFAULT_CANDLES",
    "MAX_CANDLES",
    "RULE_LABELS",
    "ChartService",
    "SignalMeter",
    "build_signal",
    "indicator_frame",
]
