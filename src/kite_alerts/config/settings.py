"""Typed configuration with startup validation.

The prototype cast env strings by ``type(default)``: a misspelled key silently
fell back to its default and a nonsensical value (``EMA_CROSS_LOOKBACK=0``)
produced a permanent dead rule with no error. Here every value is validated,
cross-field contradictions are rejected, and all problems are reported at once.

Secrets are ``SecretStr`` and can only be read through ``.get_secret_value()`` at
the point of use; ``redacted()`` produces a loggable snapshot.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ..core.calendar import SessionWindow, parse_hhmm
from ..core.errors import ConfigError
from .defaults import RULE_ENV_PREFIX, RULE_IDS, SESSION_MINUTES

_SYMBOL_RE = re.compile(r"^[A-Z]{2,6}:[A-Z0-9][A-Z0-9&._-]{0,39}$")
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Channel(str, Enum):
    CONSOLE = "console"
    TELEGRAM = "telegram"
    SLACK = "slack"
    SMTP = "smtp"


class _Env:
    """Reads typed values from a merged env mapping, tracking what was consumed."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.used: set[str] = set()

    def raw(self, key: str, default: str | None = None) -> str | None:
        self.used.add(key)
        value = self._values.get(key)
        if value is None or value.strip() == "":
            return default
        return value.strip()

    def text(self, key: str, default: str = "") -> str:
        return self.raw(key, default) or default

    def integer(self, key: str, default: int) -> int:
        value = self.raw(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{key}={value!r} is not an integer") from exc

    def number(self, key: str, default: float) -> float:
        value = self.raw(key)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{key}={value!r} is not a number") from exc

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ConfigError(f"{key}={value!r} is not a boolean (use true/false)")

    def secret(self, key: str) -> SecretStr:
        return SecretStr(self.raw(key, "") or "")

    def path(self, key: str, default: str | None = None) -> Path | None:
        value = self.raw(key, default)
        return Path(value) if value else None

    def csv(self, key: str, default: str = "") -> list[str]:
        value = self.raw(key, default) or ""
        return [item.strip() for item in value.split(",") if item.strip()]


class RuleSettings(BaseModel):
    """Per-rule switch, weight and 'must agree' flag."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    weight: float = Field(1.0, gt=0.0, le=10.0)
    required: bool = False


def _default_rules() -> dict[str, RuleSettings]:
    return {rule_id: RuleSettings() for rule_id in RULE_IDS}


class StrategyParams(BaseModel):
    """All strategy parameters. Frozen: the engine cannot mutate them mid-run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ema_fast: int = Field(9, ge=1, le=500)
    ema_slow: int = Field(21, ge=1, le=1000)
    ema_cross_lookback: int = Field(5, ge=1, le=200)
    rsi_period: int = Field(14, ge=2, le=200)
    rsi_buy_min: float = Field(55.0, ge=0.0, le=100.0)
    rsi_buy_max: float = Field(75.0, ge=0.0, le=100.0)
    rsi_sell_max: float = Field(45.0, ge=0.0, le=100.0)
    rsi_sell_min: float = Field(25.0, ge=0.0, le=100.0)
    macd_fast: int = Field(12, ge=1, le=200)
    macd_slow: int = Field(26, ge=2, le=400)
    macd_signal: int = Field(9, ge=1, le=200)
    orb_minutes: int = Field(15, ge=1, le=SESSION_MINUTES)
    orb_buffer_pct: float = Field(0.0, ge=0.0, le=10.0)
    oi_enabled: bool = True
    oi_lookback: int = Field(5, ge=1, le=200)
    oi_min_change_pct: float = Field(0.5, ge=0.0, le=100.0)
    min_agree: int = Field(4, ge=1, le=len(RULE_IDS))
    min_available_rules: int = Field(0, ge=0, le=len(RULE_IDS))
    cooldown_minutes: int = Field(15, ge=0, le=1440)
    min_side_flip_minutes: int = Field(0, ge=0, le=1440)
    warmup_bars: int = Field(40, ge=5, le=20000)
    max_bars: int = Field(500, ge=50, le=20000)
    indicator_seed: Literal["first", "sma"] = "first"
    rules: dict[str, RuleSettings] = Field(default_factory=_default_rules)

    @model_validator(mode="after")
    def _validate_consistency(self) -> StrategyParams:
        problems: list[str] = []
        if self.ema_fast >= self.ema_slow:
            problems.append(
                f"EMA_FAST ({self.ema_fast}) must be less than EMA_SLOW ({self.ema_slow})"
            )
        if self.macd_fast >= self.macd_slow:
            problems.append(
                f"MACD_FAST ({self.macd_fast}) must be less than MACD_SLOW ({self.macd_slow})"
            )
        if self.rsi_buy_min >= self.rsi_buy_max:
            problems.append(
                f"RSI_BUY_MIN ({self.rsi_buy_min}) must be below RSI_BUY_MAX ({self.rsi_buy_max})"
            )
        if self.rsi_sell_min >= self.rsi_sell_max:
            problems.append(
                f"RSI_SELL_MIN ({self.rsi_sell_min}) must be below "
                f"RSI_SELL_MAX ({self.rsi_sell_max})"
            )
        if self.rsi_sell_max >= self.rsi_buy_min:
            problems.append(
                "bullish and bearish RSI zones overlap "
                f"(RSI_SELL_MAX={self.rsi_sell_max} >= RSI_BUY_MIN={self.rsi_buy_min})"
            )
        unknown = set(self.rules) - set(RULE_IDS)
        if unknown:
            problems.append(f"unknown rule id(s) in rules config: {sorted(unknown)}")
        enabled = [rid for rid, cfg in self.rules.items() if cfg.enabled]
        if not enabled:
            problems.append("every rule is disabled; the bot could never alert")
        if self.min_agree > len(enabled):
            problems.append(
                f"MIN_AGREE={self.min_agree} exceeds the {len(enabled)} enabled rule(s) "
                f"({', '.join(enabled)}); no alert could ever fire"
            )
        if self.min_available_rules > self.min_agree:
            problems.append(
                f"MIN_AVAILABLE_RULES={self.min_available_rules} must be <= MIN_AGREE="
                f"{self.min_agree}"
            )
        if self.warmup_bars < self.min_bars_required:
            problems.append(
                f"WARMUP_BARS={self.warmup_bars} is below the {self.min_bars_required} bars the "
                "configured indicators need before they produce a value"
            )
        if self.max_bars < self.warmup_bars:
            problems.append(f"MAX_BARS={self.max_bars} must be >= WARMUP_BARS={self.warmup_bars}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def min_bars_required(self) -> int:
        """Bars needed before every enabled indicator can produce a value."""
        needs = [1]
        if self.rules.get("ema_cross", RuleSettings()).enabled:
            needs.append(self.ema_slow)
        if self.rules.get("rsi_zone", RuleSettings()).enabled:
            needs.append(self.rsi_period + 1)
        if self.rules.get("macd", RuleSettings()).enabled:
            needs.append(self.macd_slow + self.macd_signal)
        if self.rules.get("orb", RuleSettings()).enabled:
            needs.append(self.orb_minutes + 1)
        return max(needs)

    @property
    def enabled_rule_ids(self) -> tuple[str, ...]:
        return tuple(rid for rid in RULE_IDS if self.rules.get(rid, RuleSettings()).enabled)

    def with_overrides(self, overrides: Mapping[str, Any]) -> StrategyParams:
        """Return a re-validated copy with ``overrides`` applied."""
        merged = {**self.model_dump(), **dict(overrides)}
        return StrategyParams(**merged)

    def snapshot(self) -> dict[str, Any]:
        """Plain, JSON-safe parameter snapshot for the alert audit log."""
        return self.model_dump(mode="json")


class KiteCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str = ""
    api_secret: SecretStr = SecretStr("")
    access_token: SecretStr = SecretStr("")

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("KITE_API_KEY", self.api_key),
                ("KITE_API_SECRET", self.api_secret.get_secret_value()),
                ("KITE_ACCESS_TOKEN", self.access_token.get_secret_value()),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "missing credentials: "
                + ", ".join(missing)
                + ". Set them in .env (run `kite-alerts login` for the daily token)."
            )

    @property
    def token(self) -> str:
        return self.access_token.get_secret_value()


class NotifySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channels: tuple[Channel, ...] = (Channel.CONSOLE,)
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    slack_webhook_url: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_use_tls: bool = True
    retries: int = Field(4, ge=1, le=10)
    timeout_seconds: float = Field(10.0, gt=0, le=120)
    max_per_minute: int = Field(20, ge=1, le=600)
    dedup_seconds: int = Field(300, ge=0, le=86400)

    @model_validator(mode="after")
    def _validate_channels(self) -> NotifySettings:
        problems: list[str] = []
        if Channel.TELEGRAM in self.channels:
            token = self.telegram_bot_token.get_secret_value()
            if not token or ":" not in token:
                problems.append(
                    "TELEGRAM_BOT_TOKEN is missing or malformed (expected 123456:ABC...)"
                )
            if not self.telegram_chat_id:
                problems.append("TELEGRAM_CHAT_ID is required when telegram is enabled")
        if Channel.SLACK in self.channels:
            url = self.slack_webhook_url.get_secret_value()
            if not url.startswith(("https://", "http://")):
                problems.append("SLACK_WEBHOOK_URL must be an http(s) URL when slack is enabled")
        if Channel.SMTP in self.channels:
            for name, value in (
                ("SMTP_HOST", self.smtp_host),
                ("SMTP_FROM", self.smtp_from),
                ("SMTP_TO", self.smtp_to),
            ):
                if not value:
                    problems.append(f"{name} is required when smtp is enabled")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class SessionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    open_time: str = "09:15"
    close_time: str = "15:30"
    pre_open_time: str = "09:00"
    closing_end: str = "16:00"
    holiday_file: Path | None = None
    special_sessions_file: Path | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> SessionSettings:
        window = SessionWindow.parse(self.open_time, self.close_time)  # raises ConfigError
        pre_open = parse_hhmm(self.pre_open_time)
        closing = parse_hhmm(self.closing_end)
        if pre_open >= window.open_time:
            raise ValueError("SESSION_PRE_OPEN must be before SESSION_OPEN")
        if closing <= window.close_time:
            raise ValueError("SESSION_CLOSING_END must be after SESSION_CLOSE")
        return self

    def window(self) -> SessionWindow:
        return SessionWindow.parse(self.open_time, self.close_time)


class ResilienceSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    history_rate_limit_per_sec: float = Field(3.0, gt=0, le=50)
    history_max_retries: int = Field(5, ge=1, le=10)
    retry_base_seconds: float = Field(1.0, gt=0, le=30)
    retry_cap_seconds: float = Field(30.0, gt=0, le=300)
    circuit_failure_threshold: int = Field(5, ge=1, le=100)
    circuit_reset_seconds: float = Field(120.0, gt=0, le=3600)
    feed_stale_seconds: int = Field(120, ge=10, le=3600)
    symbol_stale_seconds: int = Field(600, ge=30, le=7200)
    reconnect_max_tries: int = Field(25, ge=1, le=100)
    reconnect_min_delay: int = Field(1, ge=1, le=60)
    reconnect_max_delay: int = Field(60, ge=1, le=300)
    request_timeout_seconds: float = Field(10.0, gt=0, le=120)
    tick_queue_size: int = Field(20000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def _validate_backoff(self) -> ResilienceSettings:
        if self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_CAP_SECONDS must be >= RETRY_BASE_SECONDS")
        if self.reconnect_max_delay < self.reconnect_min_delay:
            raise ValueError("RECONNECT_MAX_DELAY must be >= RECONNECT_MIN_DELAY")
        return self


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    http_enabled: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = Field(9109, ge=1, le=65535)
    http_allow_public: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False
    log_dir: Path = Path("logs")
    heartbeat_minutes: int = Field(60, ge=0, le=1440)

    @model_validator(mode="after")
    def _validate_bind(self) -> ObservabilitySettings:
        if (
            self.http_enabled
            and self.http_host not in _LOOPBACK_HOSTS
            and not self.http_allow_public
        ):
            raise ValueError(
                f"HTTP_HOST={self.http_host!r} would expose /health and /metrics publicly; "
                "bind 127.0.0.1 or set HTTP_ALLOW_PUBLIC=true deliberately"
            )
        return self


class Settings(BaseModel):
    """The complete, validated runtime configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kite: KiteCredentials = Field(default_factory=KiteCredentials)
    symbols: tuple[str, ...] = ()
    params: StrategyParams = Field(default_factory=StrategyParams)
    overrides_file: Path | None = None
    notify: NotifySettings = Field(default_factory=NotifySettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    db_path: Path = Path("data/kite_alerts.db")
    cache_dir: Path = Path("cache")
    warmup_days: int = Field(4, ge=1, le=60)
    env_warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_symbols(self) -> Settings:
        normalized: list[str] = []
        seen: set[str] = set()
        problems: list[str] = []
        for raw in self.symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if not _SYMBOL_RE.match(symbol):
                problems.append(
                    f"SYMBOLS entry {raw!r} is not EXCHANGE:TRADINGSYMBOL (e.g. NSE:RELIANCE, "
                    "NFO:NIFTY26NOVFUT or the alias NFO:NIFTY-FUT)"
                )
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        if problems:
            raise ValueError("; ".join(problems))
        if normalized != list(self.symbols):
            object.__setattr__(self, "symbols", tuple(normalized))
        return self

    def warnings_list(self) -> list[str]:
        """Non-fatal configuration smells, surfaced at startup."""
        warnings: list[str] = list(self.env_warnings)
        if self.params.warmup_bars < 60:
            warnings.append(
                f"WARMUP_BARS={self.params.warmup_bars}: the EMA/RSI seeds need about 60 bars "
                "to converge on TA-Lib/chart values; earlier alerts can differ slightly"
            )
        if self.params.max_bars < SESSION_MINUTES:
            warnings.append(
                f"MAX_BARS={self.params.max_bars} is shorter than one session "
                f"({SESSION_MINUTES} min); the opening-range rule loses its 09:15 bars late in "
                "the day"
            )
        if self.params.indicator_seed == "first":
            warnings.append(
                "INDICATOR_SEED=first reproduces the prototype's values (RSI may differ from "
                "charts by a few points for the first ~60 bars); use 'sma' for TA-Lib parity"
            )
        if (
            self.observability.heartbeat_minutes
            and Channel.CONSOLE in self.notify.channels
            and len(self.notify.channels) == 1
        ):
            warnings.append(
                "heartbeat is enabled but console is the only channel; set TELEGRAM_CHAT_ID for "
                "an unattended heartbeat"
            )
        if not self.symbols:
            warnings.append("SYMBOLS is empty: the live runner has nothing to watch")
        return warnings

    def redacted(self) -> dict[str, Any]:
        """Loggable snapshot: no secret ever appears in clear text."""
        return {
            "symbols": list(self.symbols),
            "channels": [c.value for c in self.notify.channels],
            "params": self.params.snapshot(),
            "session": {"open": self.session.open_time, "close": self.session.close_time},
            "db_path": str(self.db_path),
            "http": {
                "enabled": self.observability.http_enabled,
                "host": self.observability.http_host,
                "port": self.observability.http_port,
            },
            "api_key_hint": (self.kite.api_key[:4] + "…") if self.kite.api_key else "",
            "access_token_present": bool(self.kite.token),
            "api_secret_present": bool(self.kite.api_secret.get_secret_value()),
        }


def _rules_from_env(env: _Env) -> dict[str, RuleSettings]:
    rules: dict[str, RuleSettings] = {}
    for rule_id in RULE_IDS:
        prefix = f"{RULE_ENV_PREFIX}{rule_id.upper()}_"
        default = RuleSettings()
        rules[rule_id] = RuleSettings(
            enabled=env.flag(prefix + "ENABLED", default.enabled),
            weight=env.number(prefix + "WEIGHT", default.weight),
            required=env.flag(prefix + "REQUIRED", default.required),
        )
    return rules


def _build(env: _Env) -> dict[str, Any]:
    params = StrategyParams(
        ema_fast=env.integer("EMA_FAST", 9),
        ema_slow=env.integer("EMA_SLOW", 21),
        ema_cross_lookback=env.integer("EMA_CROSS_LOOKBACK", 5),
        rsi_period=env.integer("RSI_PERIOD", 14),
        rsi_buy_min=env.number("RSI_BUY_MIN", 55.0),
        rsi_buy_max=env.number("RSI_BUY_MAX", 75.0),
        rsi_sell_max=env.number("RSI_SELL_MAX", 45.0),
        rsi_sell_min=env.number("RSI_SELL_MIN", 25.0),
        macd_fast=env.integer("MACD_FAST", 12),
        macd_slow=env.integer("MACD_SLOW", 26),
        macd_signal=env.integer("MACD_SIGNAL", 9),
        orb_minutes=env.integer("ORB_MINUTES", 15),
        orb_buffer_pct=env.number("ORB_BUFFER_PCT", 0.0),
        oi_enabled=env.flag("OI_ENABLED", True),
        oi_lookback=env.integer("OI_LOOKBACK", 5),
        oi_min_change_pct=env.number("OI_MIN_CHANGE_PCT", 0.5),
        min_agree=env.integer("MIN_AGREE", 4),
        min_available_rules=env.integer("MIN_AVAILABLE_RULES", 0),
        cooldown_minutes=env.integer("COOLDOWN_MINUTES", 15),
        min_side_flip_minutes=env.integer("MIN_SIDE_FLIP_MINUTES", 0),
        warmup_bars=env.integer("WARMUP_BARS", 40),
        max_bars=env.integer("MAX_BARS", 500),
        indicator_seed=env.text("INDICATOR_SEED", "first").lower(),
        rules=_rules_from_env(env),
    )

    channels: list[Channel] = []
    for name in env.csv("NOTIFY_CHANNELS", "console"):
        try:
            channels.append(Channel(name.lower()))
        except ValueError as exc:
            valid = ", ".join(c.value for c in Channel)
            raise ConfigError(f"NOTIFY_CHANNELS contains {name!r}; valid values: {valid}") from exc

    notify = NotifySettings(
        channels=tuple(channels) or (Channel.CONSOLE,),
        telegram_bot_token=env.secret("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env.text("TELEGRAM_CHAT_ID"),
        slack_webhook_url=env.secret("SLACK_WEBHOOK_URL"),
        smtp_host=env.text("SMTP_HOST"),
        smtp_port=env.integer("SMTP_PORT", 587),
        smtp_user=env.text("SMTP_USER"),
        smtp_password=env.secret("SMTP_PASSWORD"),
        smtp_from=env.text("SMTP_FROM"),
        smtp_to=env.text("SMTP_TO"),
        smtp_use_tls=env.flag("SMTP_USE_TLS", True),
        retries=env.integer("NOTIFY_RETRIES", 4),
        timeout_seconds=env.number("NOTIFY_TIMEOUT_SECONDS", 10.0),
        max_per_minute=env.integer("NOTIFY_MAX_PER_MINUTE", 20),
        dedup_seconds=env.integer("NOTIFY_DEDUP_SECONDS", 300),
    )

    return {
        "kite": KiteCredentials(
            api_key=env.text("KITE_API_KEY"),
            api_secret=env.secret("KITE_API_SECRET"),
            access_token=env.secret("KITE_ACCESS_TOKEN"),
        ),
        "symbols": tuple(env.csv("SYMBOLS")),
        "params": params,
        "overrides_file": env.path("OVERRIDES_FILE"),
        "notify": notify,
        "session": SessionSettings(
            open_time=env.text("SESSION_OPEN", "09:15"),
            close_time=env.text("SESSION_CLOSE", "15:30"),
            pre_open_time=env.text("SESSION_PRE_OPEN", "09:00"),
            closing_end=env.text("SESSION_CLOSING_END", "16:00"),
            holiday_file=env.path("HOLIDAY_FILE", "config/holidays.yaml"),
            special_sessions_file=env.path("SPECIAL_SESSIONS_FILE", "config/sessions.yaml"),
        ),
        "resilience": ResilienceSettings(
            history_rate_limit_per_sec=env.number("HISTORY_RATE_LIMIT_PER_SEC", 3.0),
            history_max_retries=env.integer("HISTORY_MAX_RETRIES", 5),
            retry_base_seconds=env.number("RETRY_BASE_SECONDS", 1.0),
            retry_cap_seconds=env.number("RETRY_CAP_SECONDS", 30.0),
            circuit_failure_threshold=env.integer("CIRCUIT_FAILURE_THRESHOLD", 5),
            circuit_reset_seconds=env.number("CIRCUIT_RESET_SECONDS", 120.0),
            feed_stale_seconds=env.integer("FEED_STALE_SECONDS", 120),
            symbol_stale_seconds=env.integer("SYMBOL_STALE_SECONDS", 600),
            reconnect_max_tries=env.integer("RECONNECT_MAX_TRIES", 25),
            reconnect_min_delay=env.integer("RECONNECT_MIN_DELAY", 1),
            reconnect_max_delay=env.integer("RECONNECT_MAX_DELAY", 60),
            request_timeout_seconds=env.number("REQUEST_TIMEOUT_SECONDS", 10.0),
            tick_queue_size=env.integer("TICK_QUEUE_SIZE", 20000),
        ),
        "observability": ObservabilitySettings(
            http_enabled=env.flag("HTTP_ENABLED", True),
            http_host=env.text("HTTP_HOST", "127.0.0.1"),
            http_port=env.integer("HTTP_PORT", 9109),
            http_allow_public=env.flag("HTTP_ALLOW_PUBLIC", False),
            log_level=env.text("LOG_LEVEL", "INFO").upper(),
            log_json=env.flag("LOG_JSON", False),
            log_dir=env.path("LOG_DIR", "logs") or Path("logs"),
            heartbeat_minutes=env.integer("HEARTBEAT_MINUTES", 60),
        ),
        "db_path": env.path("DB_PATH", "data/kite_alerts.db") or Path("data/kite_alerts.db"),
        "cache_dir": env.path("CACHE_DIR", "cache") or Path("cache"),
        "warmup_days": env.integer("WARMUP_DAYS", 4),
    }


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "config"
        lines.append(f"  - {location}: {error['msg']}")
    return "invalid configuration:\n" + "\n".join(lines)


def load_settings(
    env_file: str | Path | None = ".env",
    *,
    environ: Mapping[str, str] | None = None,
    strict_env: bool = False,
) -> Settings:
    """Load settings from ``.env`` overlaid with the process environment.

    Unknown keys *inside the .env file* are reported: a typo must never silently
    keep a default. Process environment is ignored for that check because it
    legitimately contains hundreds of unrelated variables.
    """
    file_values: dict[str, str] = {}
    path = Path(env_file) if env_file else None
    if path is not None and path.exists():
        try:
            file_values = {k: v for k, v in dotenv_values(path).items() if v is not None}
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    merged: dict[str, str] = dict(file_values)
    merged.update(dict(environ if environ is not None else os.environ))

    env = _Env(merged)
    try:
        values = _build(env)
        unknown = sorted(
            key
            for key in file_values
            if key not in env.used and re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
        )
        if unknown:
            message = (
                f"unrecognised key(s) in {path}: {', '.join(unknown)} — "
                "check spelling (they would otherwise be ignored)"
            )
            if strict_env:
                raise ConfigError(message)
            values["env_warnings"] = (message,)
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(
            f"{_format_validation_error(exc)}" + (f"\n(see {path})" if path else "")
        ) from exc
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
