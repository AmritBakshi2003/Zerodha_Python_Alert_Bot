"""Central configuration. Everything tunable lives in .env -> Params."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _get(name, default, cast=str):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return cast(v)


@dataclass(frozen=True)
class Params:
    """Strategy parameters. Frozen so the engine can't mutate them mid-run."""
    ema_fast: int = 9
    ema_slow: int = 21
    ema_cross_lookback: int = 5
    rsi_period: int = 14
    rsi_buy_min: float = 55
    rsi_buy_max: float = 75
    rsi_sell_max: float = 45
    rsi_sell_min: float = 25
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    orb_minutes: int = 15
    orb_buffer_pct: float = 0.0
    oi_enabled: bool = True
    oi_lookback: int = 5
    oi_min_change_pct: float = 0.5
    min_agree: int = 4
    cooldown_minutes: int = 15
    warmup_bars: int = 40
    max_bars: int = 500


def load_params() -> Params:
    d = Params()
    return Params(**{f: _get(f.upper(), getattr(d, f), type(getattr(d, f)))
                     for f in d.__dataclass_fields__})


def load_settings() -> dict:
    return {
        "api_key": _get("KITE_API_KEY", ""),
        "api_secret": _get("KITE_API_SECRET", ""),
        "access_token": _get("KITE_ACCESS_TOKEN", ""),
        "symbols": [s.strip() for s in _get("SYMBOLS", "").split(",") if s.strip()],
        "channels": [c.strip().lower() for c in _get("NOTIFY_CHANNELS", "console").split(",") if c.strip()],
        "telegram_token": _get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat": _get("TELEGRAM_CHAT_ID", ""),
        "slack_webhook": _get("SLACK_WEBHOOK_URL", ""),
    }
