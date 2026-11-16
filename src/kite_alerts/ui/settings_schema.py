"""Field metadata for the settings form, derived from the pydantic models.

The dashboard must not keep a second hand-written copy of the configuration. This
module declares only the things the models cannot express: which environment
variable maps to which model path, and the plain-language label and help text.

Everything else - the default value, numeric bounds, the set of allowed choices,
whether a field is a secret - is read from ``Settings`` itself, so a new field or
a tightened bound appears in the UI without touching JavaScript. A test asserts
that the env mapping covers every key ``settings._build`` consumes.

The ``Kite Connect`` fields are listed separately: they belong to the Connect
wizard, not the Settings page, and are write-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, cast, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import FieldInfo

from ..config.defaults import RULE_ENV_PREFIX, RULE_IDS
from ..config.settings import Channel, RuleSettings, Settings

SETTINGS_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "instruments",
        "Instruments",
        "What the bot watches, and how much history it warms up on.",
    ),
    (
        "rules",
        "Alert rules",
        "The thresholds that decide whether a candle is worth an alert.",
    ),
    (
        "notifications",
        "Notifications",
        "Where an alert goes. The terminal window is always included.",
    ),
    (
        "advanced",
        "Advanced",
        "Session times, storage, the health server and resilience. Defaults are fine "
        "for most people.",
    ),
)

CONNECT_GROUP = ("connect", "Kite Connect", "Your own Kite Connect app.")

#: Fields that must never be returned by an endpoint.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "KITE_ACCESS_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SLACK_WEBHOOK_URL",
        "SMTP_PASSWORD",
    }
)

#: Secrets the user may type on the Settings page (the Kite ones are on Connect).
SETTINGS_SECRET_KEYS: frozenset[str] = frozenset(
    {"TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "SMTP_PASSWORD"}
)

#: Values the token field accepts: only a clear, never a new token.
CONNECT_ONLY_SECRETS: frozenset[str] = frozenset({"KITE_ACCESS_TOKEN"})

RULE_LABELS: dict[str, str] = {
    "ema_cross": "EMA cross",
    "rsi_zone": "RSI zone",
    "macd": "MACD",
    "orb": "Opening range",
    "oi": "Open interest",
}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One editable environment variable, plus where it lives in the model."""

    env: str
    path: tuple[str, ...]
    group: str
    label: str
    help: str
    unit: str = ""
    editable: bool = True
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class FieldMeta:
    """Runtime metadata resolved from pydantic for one :class:`FieldSpec`."""

    spec: FieldSpec
    kind: str
    default: Any
    minimum: float | int | None
    maximum: float | int | None
    choices: tuple[str, ...]
    secret: bool
    is_set: bool
    value: Any


_DEFAULT_SETTINGS = Settings()


def _settings_fields() -> tuple[FieldSpec, ...]:
    specs: list[FieldSpec] = [
        # ------------------------------------------------------------ instruments
        FieldSpec(
            "SYMBOLS",
            ("symbols",),
            "instruments",
            "Instruments to watch",
            "Comma-separated, as EXCHANGE:SYMBOL — for example NSE:RELIANCE, NFO:NIFTY-FUT. "
            "Futures can use the NIFTY-FUT alias and roll automatically.",
            unit="list",
        ),
        FieldSpec(
            "WARMUP_DAYS",
            ("warmup_days",),
            "instruments",
            "Days of history to load",
            "How much earlier data the bot loads before the open so its indicators are ready.",
            unit="days",
        ),
        FieldSpec(
            "OVERRIDES_FILE",
            ("overrides_file",),
            "instruments",
            "Per-symbol overrides file",
            "Optional YAML file with different thresholds for individual instruments. "
            "Leave empty to use one set of rules for everything.",
            unit="path",
            advanced=True,
        ),
        # ------------------------------------------------------------------ rules
        FieldSpec("EMA_FAST", ("params", "ema_fast"), "rules", "Fast EMA", "Short moving average."),
        FieldSpec("EMA_SLOW", ("params", "ema_slow"), "rules", "Slow EMA", "Long moving average."),
        FieldSpec(
            "EMA_CROSS_LOOKBACK",
            ("params", "ema_cross_lookback"),
            "rules",
            "EMA cross lookback",
            "How many candles back a crossover still counts.",
            unit="candles",
        ),
        FieldSpec(
            "RSI_PERIOD",
            ("params", "rsi_period"),
            "rules",
            "RSI period",
            "RSI lookback.",
            unit="candles",
        ),
        FieldSpec(
            "RSI_BUY_MIN",
            ("params", "rsi_buy_min"),
            "rules",
            "RSI buy zone starts",
            "Lowest RSI that supports a BUY alert.",
        ),
        FieldSpec(
            "RSI_BUY_MAX",
            ("params", "rsi_buy_max"),
            "rules",
            "RSI buy zone ends",
            "Highest RSI that supports a BUY alert.",
        ),
        FieldSpec(
            "RSI_SELL_MIN",
            ("params", "rsi_sell_min"),
            "rules",
            "RSI sell zone starts",
            "Lowest RSI that supports a SELL alert.",
        ),
        FieldSpec(
            "RSI_SELL_MAX",
            ("params", "rsi_sell_max"),
            "rules",
            "RSI sell zone ends",
            "Highest RSI that supports a SELL alert.",
        ),
        FieldSpec(
            "MACD_FAST",
            ("params", "macd_fast"),
            "rules",
            "MACD fast",
            "Fast MACD period.",
            unit="candles",
        ),
        FieldSpec(
            "MACD_SLOW",
            ("params", "macd_slow"),
            "rules",
            "MACD slow",
            "Slow MACD period.",
            unit="candles",
        ),
        FieldSpec(
            "MACD_SIGNAL",
            ("params", "macd_signal"),
            "rules",
            "MACD signal",
            "Signal-line period.",
            unit="candles",
        ),
        FieldSpec(
            "ORB_MINUTES",
            ("params", "orb_minutes"),
            "rules",
            "Opening range length",
            "Minutes after the open that define the opening range.",
            unit="minutes",
        ),
        FieldSpec(
            "ORB_BUFFER_PCT",
            ("params", "orb_buffer_pct"),
            "rules",
            "Opening range buffer",
            "Extra distance beyond the range that price must clear before the rule agrees.",
            unit="%",
        ),
        FieldSpec(
            "OI_ENABLED",
            ("params", "oi_enabled"),
            "rules",
            "Use open interest",
            "Turn the open-interest rule on or off.",
        ),
        FieldSpec(
            "OI_LOOKBACK",
            ("params", "oi_lookback"),
            "rules",
            "Open-interest lookback",
            "Candles used to measure the OI change.",
            unit="candles",
        ),
        FieldSpec(
            "OI_MIN_CHANGE_PCT",
            ("params", "oi_min_change_pct"),
            "rules",
            "Open-interest move",
            "Smallest OI change the rule treats as meaningful.",
            unit="%",
        ),
        FieldSpec(
            "MIN_AGREE",
            ("params", "min_agree"),
            "rules",
            "Rules that must agree",
            "How many rules must point the same way before an alert fires.",
        ),
        FieldSpec(
            "MIN_AVAILABLE_RULES",
            ("params", "min_available_rules"),
            "rules",
            "Minimum rules available",
            "Skip a candle when fewer rules than this have enough data. 0 disables the check.",
        ),
        FieldSpec(
            "COOLDOWN_MINUTES",
            ("params", "cooldown_minutes"),
            "rules",
            "Cooldown between alerts",
            "No new alert for the same instrument during this many minutes.",
            unit="minutes",
        ),
        FieldSpec(
            "MIN_SIDE_FLIP_MINUTES",
            ("params", "min_side_flip_minutes"),
            "rules",
            "Minimum side-flip gap",
            "Extra wait before switching from BUY to SELL on the same instrument. 0 disables it.",
            unit="minutes",
        ),
        FieldSpec(
            "WARMUP_BARS",
            ("params", "warmup_bars"),
            "advanced",
            "Warm-up bars",
            "Bars loaded before the bot is allowed to alert. Must be at least what "
            "the indicators need.",
            unit="bars",
            advanced=True,
        ),
        FieldSpec(
            "MAX_BARS",
            ("params", "max_bars"),
            "advanced",
            "Maximum bars kept",
            "Upper bound on the in-memory history per instrument.",
            unit="bars",
            advanced=True,
        ),
        FieldSpec(
            "INDICATOR_SEED",
            ("params", "indicator_seed"),
            "rules",
            "Indicator seeding",
            "How indicator averages are seeded. 'first' reproduces the original prototype; "
            "'sma' matches TA-Lib and charting tools more closely.",
            advanced=True,
        ),
        # ---------------------------------------------------------- notifications
        FieldSpec(
            "NOTIFY_CHANNELS",
            ("notify", "channels"),
            "notifications",
            "Channels",
            "The terminal window is always on. Add Telegram or Slack to reach your phone.",
        ),
        FieldSpec(
            "TELEGRAM_BOT_TOKEN",
            ("notify", "telegram_bot_token"),
            "notifications",
            "Telegram bot token",
            "From @BotFather. Stored in .env and never shown again.",
        ),
        FieldSpec(
            "TELEGRAM_CHAT_ID",
            ("notify", "telegram_chat_id"),
            "notifications",
            "Telegram chat id",
            "Who receives the message. Use “Find my chat id” after pressing Start on your bot.",
        ),
        FieldSpec(
            "SLACK_WEBHOOK_URL",
            ("notify", "slack_webhook_url"),
            "notifications",
            "Slack webhook URL",
            "An incoming-webhook URL. Stored in .env and never shown again.",
        ),
        FieldSpec(
            "SMTP_HOST",
            ("notify", "smtp_host"),
            "notifications",
            "SMTP host",
            "Email server name.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_PORT",
            ("notify", "smtp_port"),
            "notifications",
            "SMTP port",
            "Usually 587 with TLS.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_USER",
            ("notify", "smtp_user"),
            "notifications",
            "SMTP username",
            "Login for the mail server.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_PASSWORD",
            ("notify", "smtp_password"),
            "notifications",
            "SMTP password",
            "Stored in .env and never shown again.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_FROM",
            ("notify", "smtp_from"),
            "notifications",
            "Email from",
            "Sender address.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_TO",
            ("notify", "smtp_to"),
            "notifications",
            "Email to",
            "Recipient address.",
            advanced=True,
        ),
        FieldSpec(
            "SMTP_USE_TLS",
            ("notify", "smtp_use_tls"),
            "notifications",
            "Use TLS",
            "Encrypt the connection to the mail server.",
            advanced=True,
        ),
        FieldSpec(
            "NOTIFY_RETRIES",
            ("notify", "retries"),
            "notifications",
            "Delivery retries",
            "Attempts per channel before giving up.",
            advanced=True,
        ),
        FieldSpec(
            "NOTIFY_TIMEOUT_SECONDS",
            ("notify", "timeout_seconds"),
            "notifications",
            "Delivery timeout",
            "Seconds to wait for one channel.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "NOTIFY_MAX_PER_MINUTE",
            ("notify", "max_per_minute"),
            "notifications",
            "Alerts per minute",
            "Hard cap so a misconfiguration cannot flood you.",
            advanced=True,
        ),
        FieldSpec(
            "NOTIFY_DEDUP_SECONDS",
            ("notify", "dedup_seconds"),
            "notifications",
            "Duplicate window",
            "Ignore an identical alert repeated inside this many seconds.",
            unit="seconds",
            advanced=True,
        ),
        # --------------------------------------------------------------- advanced
        FieldSpec(
            "SESSION_OPEN",
            ("session", "open_time"),
            "advanced",
            "Session opens",
            "HH:MM in IST.",
            advanced=True,
        ),
        FieldSpec(
            "SESSION_CLOSE",
            ("session", "close_time"),
            "advanced",
            "Session closes",
            "HH:MM in IST.",
            advanced=True,
        ),
        FieldSpec(
            "SESSION_PRE_OPEN",
            ("session", "pre_open_time"),
            "advanced",
            "Pre-open starts",
            "HH:MM in IST; the NSE pre-open runs 09:00-09:15.",
            advanced=True,
        ),
        FieldSpec(
            "SESSION_CLOSING_END",
            ("session", "closing_end"),
            "advanced",
            "Closing window ends",
            "HH:MM in IST; 15:30-16:00 is reported as closing, not traded.",
            advanced=True,
        ),
        FieldSpec(
            "HOLIDAY_FILE",
            ("session", "holiday_file"),
            "advanced",
            "Holiday file",
            "Optional YAML list of market holidays (see scripts/refresh_holidays.py).",
            advanced=True,
        ),
        FieldSpec(
            "SPECIAL_SESSIONS_FILE",
            ("session", "special_sessions_file"),
            "advanced",
            "Special sessions file",
            "Optional YAML list of dated session overrides (muhurat trading).",
            advanced=True,
        ),
        FieldSpec(
            "DB_PATH",
            ("db_path",),
            "advanced",
            "Alert database",
            "Where recorded alerts are stored.",
            advanced=True,
        ),
        FieldSpec(
            "CACHE_DIR",
            ("cache_dir",),
            "advanced",
            "Cache folder",
            "Instrument and history cache.",
            advanced=True,
        ),
        FieldSpec(
            "HTTP_ENABLED",
            ("observability", "http_enabled"),
            "advanced",
            "Health server",
            "Lets the dashboard see whether the bot is running.",
            advanced=True,
        ),
        FieldSpec(
            "HTTP_HOST",
            ("observability", "http_host"),
            "advanced",
            "Health host",
            "Keep this on 127.0.0.1.",
            advanced=True,
        ),
        FieldSpec(
            "HTTP_PORT",
            ("observability", "http_port"),
            "advanced",
            "Health port",
            "Port of the bot's /health endpoint.",
            advanced=True,
        ),
        FieldSpec(
            "HTTP_ALLOW_PUBLIC",
            ("observability", "http_allow_public"),
            "advanced",
            "Allow a public health bind",
            "Only for a deliberately public deployment. The dashboard never needs it.",
            advanced=True,
        ),
        FieldSpec(
            "LOG_LEVEL",
            ("observability", "log_level"),
            "advanced",
            "Log level",
            "How much the bot writes to its log.",
            advanced=True,
        ),
        FieldSpec(
            "LOG_JSON",
            ("observability", "log_json"),
            "advanced",
            "JSON logs",
            "Machine-readable logs for a log collector.",
            advanced=True,
        ),
        FieldSpec(
            "LOG_DIR",
            ("observability", "log_dir"),
            "advanced",
            "Log folder",
            "Where rotating logs go.",
            advanced=True,
        ),
        FieldSpec(
            "HEARTBEAT_MINUTES",
            ("observability", "heartbeat_minutes"),
            "advanced",
            "Heartbeat",
            "Send a quiet “still alive” note this often. 0 disables it.",
            unit="minutes",
            advanced=True,
        ),
        FieldSpec(
            "HISTORY_RATE_LIMIT_PER_SEC",
            ("resilience", "history_rate_limit_per_sec"),
            "advanced",
            "History requests per second",
            "Kite allows a limited number of historical-data calls.",
            advanced=True,
        ),
        FieldSpec(
            "HISTORY_MAX_RETRIES",
            ("resilience", "history_max_retries"),
            "advanced",
            "History retries",
            "Attempts for a failed historical-data call.",
            advanced=True,
        ),
        FieldSpec(
            "RETRY_BASE_SECONDS",
            ("resilience", "retry_base_seconds"),
            "advanced",
            "Retry base delay",
            "First backoff delay.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "RETRY_CAP_SECONDS",
            ("resilience", "retry_cap_seconds"),
            "advanced",
            "Retry cap",
            "Largest backoff delay.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "CIRCUIT_FAILURE_THRESHOLD",
            ("resilience", "circuit_failure_threshold"),
            "advanced",
            "Circuit-breaker threshold",
            "Consecutive failures before the bot pauses a feed.",
            advanced=True,
        ),
        FieldSpec(
            "CIRCUIT_RESET_SECONDS",
            ("resilience", "circuit_reset_seconds"),
            "advanced",
            "Circuit-breaker reset",
            "How long the pause lasts.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "FEED_STALE_SECONDS",
            ("resilience", "feed_stale_seconds"),
            "advanced",
            "Feed staleness",
            "Warn when no ticks arrive for this long.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "SYMBOL_STALE_SECONDS",
            ("resilience", "symbol_stale_seconds"),
            "advanced",
            "Instrument staleness",
            "Warn when one instrument goes quiet for this long.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "RECONNECT_MAX_TRIES",
            ("resilience", "reconnect_max_tries"),
            "advanced",
            "Reconnect attempts",
            "How often to retry a dropped feed.",
            advanced=True,
        ),
        FieldSpec(
            "RECONNECT_MIN_DELAY",
            ("resilience", "reconnect_min_delay"),
            "advanced",
            "Reconnect minimum delay",
            "Seconds before the first retry.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "RECONNECT_MAX_DELAY",
            ("resilience", "reconnect_max_delay"),
            "advanced",
            "Reconnect maximum delay",
            "Largest delay between retries.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "REQUEST_TIMEOUT_SECONDS",
            ("resilience", "request_timeout_seconds"),
            "advanced",
            "Request timeout",
            "Timeout for a Kite API request.",
            unit="seconds",
            advanced=True,
        ),
        FieldSpec(
            "TICK_QUEUE_SIZE",
            ("resilience", "tick_queue_size"),
            "advanced",
            "Tick queue size",
            "Buffer between the websocket and the engine.",
            advanced=True,
        ),
    ]

    for rule_id in RULE_IDS:
        label = RULE_LABELS.get(rule_id, rule_id)
        prefix = f"{RULE_ENV_PREFIX}{rule_id.upper()}_"
        base = ("params", "rules", rule_id)
        specs.extend(
            [
                FieldSpec(
                    prefix + "ENABLED",
                    (*base, "enabled"),
                    "rules",
                    f"{label}: use this rule",
                    "Turn this rule's vote on or off.",
                ),
                FieldSpec(
                    prefix + "WEIGHT",
                    (*base, "weight"),
                    "rules",
                    f"{label}: weight",
                    "How much this rule counts when votes are added up.",
                    unit="weight",
                ),
                FieldSpec(
                    prefix + "REQUIRED",
                    (*base, "required"),
                    "rules",
                    f"{label}: must agree",
                    "When on, no alert fires unless this rule agrees.",
                ),
            ]
        )
    return tuple(specs)


SETTINGS_SPECS: tuple[FieldSpec, ...] = _settings_fields()

CONNECT_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "KITE_API_KEY",
        ("kite", "api_key"),
        "connect",
        "API key",
        "From your own Kite Connect app. Stored in .env and never shown again.",
    ),
    FieldSpec(
        "KITE_API_SECRET",
        ("kite", "api_secret"),
        "connect",
        "API secret",
        "From the same Kite Connect app. Stored in .env and never shown again.",
    ),
    FieldSpec(
        "KITE_ACCESS_TOKEN",
        ("kite", "access_token"),
        "connect",
        "Access token",
        "Created by the daily Zerodha login. You can clear it here; the Connect button "
        "sets a new one.",
        editable=False,
    ),
)

ALL_SPECS: tuple[FieldSpec, ...] = SETTINGS_SPECS + CONNECT_SPECS
SPEC_BY_ENV: dict[str, FieldSpec] = {spec.env: spec for spec in ALL_SPECS}
ENV_KEYS: frozenset[str] = frozenset(SPEC_BY_ENV)


# --------------------------------------------------------------------------- pydantic


def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _model_fields(path: tuple[str, ...]) -> FieldInfo:
    """Resolve one model path to its pydantic ``FieldInfo`` (rules included)."""
    if path[:2] == ("params", "rules"):
        return RuleSettings.model_fields[path[3]]
    model: Any = Settings
    for part in path[:-1]:
        model = _unwrap_optional(model.model_fields[part].annotation)
    return cast(FieldInfo, model.model_fields[path[-1]])


def _default_value(path: tuple[str, ...]) -> Any:
    value: Any = _DEFAULT_SETTINGS
    for part in path:
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    if isinstance(value, SecretStr):
        return ""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, tuple):
        return [item.value if isinstance(item, Channel) else str(item) for item in value]
    if isinstance(value, Channel):
        return value.value
    return "" if value is None else str(value)


def _bounds(info: FieldInfo) -> tuple[float | int | None, float | int | None]:
    minimum: float | int | None = None
    maximum: float | int | None = None
    for constraint in info.metadata:
        if hasattr(constraint, "ge"):
            minimum = constraint.ge
        elif hasattr(constraint, "gt"):
            minimum = constraint.gt
        if hasattr(constraint, "le"):
            maximum = constraint.le
        elif hasattr(constraint, "lt"):
            maximum = constraint.lt
    return minimum, maximum


def _choices(annotation: Any) -> tuple[str, ...]:
    annotation = _unwrap_optional(annotation)
    if get_origin(annotation) is Literal:
        return tuple(str(arg) for arg in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Channel):
        return tuple(channel.value for channel in Channel)
    if get_origin(annotation) is tuple:
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], Channel):
            return tuple(channel.value for channel in Channel)
    return ()


def _kind(spec: FieldSpec, info: FieldInfo, secret: bool) -> str:
    if secret:
        return "secret"
    annotation = _unwrap_optional(info.annotation)
    origin = get_origin(annotation)
    if origin is tuple and get_args(annotation):
        inner = get_args(annotation)[0]
        if isinstance(inner, type) and issubclass(inner, Channel):
            return "multi"
        return "list"
    if get_origin(annotation) is Literal:
        return "choice"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is Path:
        return "path"
    if spec.unit == "list":
        return "list"
    return "text"


def _current_value(settings: Settings | None, path: tuple[str, ...]) -> Any:
    if settings is None:
        return _default_value(path)
    value: Any = settings
    for part in path:
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    if isinstance(value, SecretStr):
        return ""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [item.value if isinstance(item, Channel) else str(item) for item in value]
    return value


def _secret_is_set(settings: Settings | None, path: tuple[str, ...]) -> bool:
    if settings is None:
        return False
    value: Any = settings
    for part in path:
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value())
    return bool(value)


def field_metas(
    settings: Settings | None, *, groups: Sequence[str] | None = None
) -> list[FieldMeta]:
    """Resolve every spec to its runtime metadata, in declaration order."""
    metas: list[FieldMeta] = []
    for spec in ALL_SPECS:
        if groups is not None and spec.group not in groups:
            continue
        info = _model_fields(spec.path)
        secret = spec.env in SECRET_KEYS
        minimum, maximum = _bounds(info)
        metas.append(
            FieldMeta(
                spec=spec,
                kind=_kind(spec, info, secret),
                default=_default_value(spec.path),
                minimum=minimum,
                maximum=maximum,
                choices=_choices(info.annotation),
                secret=secret,
                is_set=_secret_is_set(settings, spec.path),
                value="" if secret else _current_value(settings, spec.path),
            )
        )
    return metas


def field_payload(meta: FieldMeta, *, for_settings_page: bool) -> dict[str, Any]:
    """JSON-safe description of one field. Never contains a secret value."""
    spec = meta.spec
    payload: dict[str, Any] = {
        "key": spec.env,
        "label": spec.label,
        "help": spec.help,
        "group": spec.group,
        "type": meta.kind,
        "default": meta.default,
        "min": meta.minimum,
        "max": meta.maximum,
        "choices": list(meta.choices),
        "secret": meta.secret,
        "is_set": meta.is_set,
        "editable": spec.editable,
        "advanced": spec.advanced,
        "unit": spec.unit,
    }
    if not meta.secret:
        payload["value"] = meta.value
    if spec.env in CONNECT_ONLY_SECRETS:
        payload["connect_only"] = True
    if for_settings_page and spec.env in SETTINGS_SECRET_KEYS:
        payload["settings_secret"] = True
    return payload


def settings_payload(
    settings: Settings | None,
    *,
    settings_error: str | None,
    env_file: str,
    connect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole Settings page, grouped, with metadata instead of a hand schema."""
    by_group: dict[str, list[dict[str, Any]]] = {gid: [] for gid, _title, _d in SETTINGS_GROUPS}
    for meta in field_metas(settings, groups=[gid for gid, _t, _d in SETTINGS_GROUPS]):
        by_group[meta.spec.group].append(field_payload(meta, for_settings_page=True))

    groups = [
        {
            "id": gid,
            "title": title,
            "description": description,
            "fields": by_group[gid],
        }
        for gid, title, description in SETTINGS_GROUPS
    ]
    warnings: list[str] = settings.warnings_list() if settings is not None else []
    return {
        "ok": settings is not None,
        "groups": groups,
        "env_file": env_file,
        "settings_error": settings_error,
        "warnings": warnings,
        "applies_notice": "Changes apply the next time the bot starts. The dashboard and "
        "the practice replay pick them up immediately.",
        "connect": connect or {},
    }


def connect_payload(settings: Settings | None, *, settings_error: str | None) -> dict[str, Any]:
    """Write-only Kite credential fields for the Connect wizard."""
    fields = [
        field_payload(meta, for_settings_page=False)
        for meta in field_metas(settings, groups=["connect"])
    ]
    return {
        "fields": fields,
        "settings_error": settings_error,
    }


RULE_ATTR_SUFFIX: dict[str, str] = {
    "enabled": "ENABLED",
    "weight": "WEIGHT",
    "required": "REQUIRED",
}


def _bare_field_map() -> dict[str, str]:
    """Field name -> env key, but only when the name is unambiguous."""
    seen: dict[str, list[str]] = {}
    for spec in SETTINGS_SPECS:
        seen.setdefault(spec.path[-1], []).append(spec.env)
    return {name: keys[0] for name, keys in seen.items() if len(keys) == 1}


_BARE_FIELD_KEYS = _bare_field_map()


def key_for_path(path: Sequence[Any]) -> str | None:
    """Map a pydantic error location to the environment key the form shows.

    ``Settings`` nests models, but pydantic validates each sub-model separately,
    so the same error can arrive as ``params.ema_fast`` or just ``ema_fast``.
    Both, and ``rules.rsi_zone.weight``, resolve to the form field.
    """
    parts = tuple(str(part) for part in path)
    if parts[:1] == ("params",):
        parts = parts[1:]
    if parts[:1] == ("rules",) and len(parts) >= 3:
        rule_id, attribute = parts[1], parts[2]
        suffix = RULE_ATTR_SUFFIX.get(attribute)
        if suffix:
            candidate = f"{RULE_ENV_PREFIX}{rule_id.upper()}_{suffix}"
            if candidate in SPEC_BY_ENV:
                return candidate
        return None
    for spec in SETTINGS_SPECS:
        if spec.path == parts or (spec.path[0] == "params" and spec.path[1:] == parts):
            return spec.env
    if len(parts) == 1:
        return _BARE_FIELD_KEYS.get(parts[0])
    return None


def field_label(key: str | None) -> str:
    if key and key in SPEC_BY_ENV:
        return SPEC_BY_ENV[key].label
    return "Settings"


__all__ = [
    "ALL_SPECS",
    "CONNECT_ONLY_SECRETS",
    "CONNECT_SPECS",
    "ENV_KEYS",
    "RULE_LABELS",
    "SECRET_KEYS",
    "SETTINGS_GROUPS",
    "SETTINGS_SECRET_KEYS",
    "SETTINGS_SPECS",
    "SPEC_BY_ENV",
    "FieldMeta",
    "FieldSpec",
    "connect_payload",
    "field_label",
    "field_metas",
    "field_payload",
    "key_for_path",
    "settings_payload",
]
