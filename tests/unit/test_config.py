"""Configuration: validation, actionable errors, secret safety, overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import write_env
from kite_alerts.config.defaults import RULE_IDS
from kite_alerts.config.overrides import SymbolOverrides
from kite_alerts.config.settings import (
    Channel,
    KiteCredentials,
    NotifySettings,
    ObservabilitySettings,
    RuleSettings,
    SessionSettings,
    Settings,
    StrategyParams,
    load_settings,
)
from kite_alerts.core.errors import ConfigError

MINIMAL = {
    "KITE_API_KEY": "key",
    "KITE_API_SECRET": "secret",
    "KITE_ACCESS_TOKEN": "token",
    "SYMBOLS": "NSE:INFY,NSE:TCS",
}


def env_file(tmp_path: Path, **overrides: str) -> Path:
    values = dict(MINIMAL)
    values.update(overrides)
    return write_env(tmp_path / ".env", values)


class TestLoading:
    def test_defaults_match_the_prototype(self, tmp_path: Path) -> None:
        settings = load_settings(env_file(tmp_path), environ={})
        assert settings.params.ema_fast == 9
        assert settings.params.ema_slow == 21
        assert settings.params.min_agree == 4
        assert settings.params.cooldown_minutes == 15
        assert settings.params.warmup_bars == 40
        assert settings.params.max_bars == 500
        assert settings.params.indicator_seed == "first"

    def test_symbols_are_normalised_and_deduplicated(self, tmp_path: Path) -> None:
        path = env_file(tmp_path, SYMBOLS="nse:infy, NSE:TCS , nse:INFY")
        settings = load_settings(path, environ={})
        assert settings.symbols == ("NSE:INFY", "NSE:TCS")

    def test_process_environment_overrides_the_file(self, tmp_path: Path) -> None:
        settings = load_settings(env_file(tmp_path), environ={"MIN_AGREE": "3"})
        assert settings.params.min_agree == 3

    def test_bool_and_float_parsing(self, tmp_path: Path) -> None:
        path = env_file(tmp_path, OI_ENABLED="no", ORB_BUFFER_PCT="0.25")
        settings = load_settings(path, environ={})
        assert settings.params.oi_enabled is False
        assert settings.params.orb_buffer_pct == 0.25

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        settings = load_settings(tmp_path / "absent.env", environ={})
        assert settings.symbols == ()
        assert any("SYMBOLS is empty" in warning for warning in settings.warnings_list())

    def test_unknown_key_is_reported(self, tmp_path: Path) -> None:
        settings = load_settings(env_file(tmp_path, EMA_FASTT="9"), environ={})
        assert any("EMA_FASTT" in warning for warning in settings.warnings_list())

    def test_unknown_key_can_be_fatal(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="EMA_FASTT"):
            load_settings(env_file(tmp_path, EMA_FASTT="9"), environ={}, strict_env=True)

    def test_non_numeric_value_names_the_key(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="EMA_FAST"):
            load_settings(env_file(tmp_path, EMA_FAST="fast"), environ={})

    def test_invalid_boolean_names_the_key(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="OI_ENABLED"):
            load_settings(env_file(tmp_path, OI_ENABLED="maybe"), environ={})


class TestValidation:
    def test_ema_fast_must_be_below_slow(self) -> None:
        with pytest.raises(ValidationError, match="EMA_FAST"):
            StrategyParams(ema_fast=30, ema_slow=10)

    def test_rsi_zones_must_not_overlap(self) -> None:
        with pytest.raises(ValidationError, match="overlap"):
            StrategyParams(rsi_sell_max=60, rsi_buy_min=55)

    def test_rsi_band_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError, match="RSI_BUY_MIN"):
            StrategyParams(rsi_buy_min=80, rsi_buy_max=70)

    def test_min_agree_cannot_exceed_enabled_rules(self) -> None:
        rules = {rule_id: RuleSettings(enabled=(rule_id == "ema_cross")) for rule_id in RULE_IDS}
        with pytest.raises(ValidationError, match="MIN_AGREE"):
            StrategyParams(min_agree=4, rules=rules)

    def test_all_rules_disabled_is_rejected(self) -> None:
        rules = {rule_id: RuleSettings(enabled=False) for rule_id in RULE_IDS}
        with pytest.raises(ValidationError, match="disabled"):
            StrategyParams(rules=rules)

    def test_warmup_below_indicator_requirement_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="WARMUP_BARS"):
            StrategyParams(warmup_bars=10)

    def test_max_bars_must_cover_warmup(self) -> None:
        with pytest.raises(ValidationError, match="MAX_BARS"):
            StrategyParams(warmup_bars=400, max_bars=100)

    def test_unknown_rule_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown rule"):
            StrategyParams(rules={**{r: RuleSettings() for r in RULE_IDS}, "magic": RuleSettings()})

    def test_invalid_indicator_seed(self) -> None:
        with pytest.raises(ValidationError):
            StrategyParams(indicator_seed="talib")  # type: ignore[arg-type]

    def test_bad_symbol_format(self) -> None:
        with pytest.raises(ValidationError, match="SYMBOLS"):
            Settings(symbols=("INFY",))

    def test_public_http_bind_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="HTTP_HOST"):
            ObservabilitySettings(http_host="0.0.0.0")

    def test_public_bind_can_be_allowed_explicitly(self) -> None:
        settings = ObservabilitySettings(http_host="0.0.0.0", http_allow_public=True)
        assert settings.http_allow_public is True

    def test_telegram_requires_token_and_chat(self) -> None:
        with pytest.raises(ValidationError, match="TELEGRAM"):
            NotifySettings(channels=(Channel.TELEGRAM,))

    def test_smtp_requires_endpoints(self) -> None:
        with pytest.raises(ValidationError, match="SMTP"):
            NotifySettings(channels=(Channel.SMTP,))

    def test_session_window_must_be_ordered(self) -> None:
        with pytest.raises(ConfigError, match="before close"):
            SessionSettings(open_time="15:30", close_time="09:15")

    def test_credentials_are_required_for_live(self) -> None:
        with pytest.raises(ConfigError, match="KITE_ACCESS_TOKEN"):
            KiteCredentials(api_key="k", api_secret="s").require()


class TestSecrets:
    def test_secrets_never_appear_in_redacted_output(self, tmp_path: Path) -> None:
        secret_value = "s3cr3t-value-xyz"
        token_value = "123456:abcdefghijklmnopqrstuvwxyz"
        values = dict(MINIMAL)
        values["KITE_API_SECRET"] = secret_value
        values["KITE_ACCESS_TOKEN"] = token_value
        values["TELEGRAM_BOT_TOKEN"] = token_value
        path = write_env(tmp_path / ".env", values)
        settings = load_settings(path, environ={})

        dumped = json.dumps(settings.redacted())
        assert secret_value not in dumped
        assert token_value not in dumped
        assert settings.kite.api_secret.get_secret_value() == secret_value

    def test_repr_does_not_leak(self, tmp_path: Path) -> None:
        secret_value = "s3cr3t-value-xyz"
        values = dict(MINIMAL)
        values["KITE_API_SECRET"] = secret_value
        path = write_env(tmp_path / ".env", values)
        settings = load_settings(path, environ={})
        assert secret_value not in repr(settings)
        assert secret_value not in repr(settings.kite)


class TestWarnings:
    def test_warmup_warning_explains_indicator_convergence(self, tmp_path: Path) -> None:
        settings = load_settings(env_file(tmp_path), environ={})
        warnings = " ".join(settings.warnings_list())
        assert "WARMUP_BARS" in warnings
        assert "INDICATOR_SEED" in warnings

    def test_short_max_bars_warning(self, tmp_path: Path) -> None:
        settings = load_settings(env_file(tmp_path, MAX_BARS="200"), environ={})
        assert any("MAX_BARS" in warning for warning in settings.warnings_list())


class TestOverrides:
    def test_defaults_and_per_symbol(self, tmp_path: Path) -> None:
        path = tmp_path / "overrides.yaml"
        path.write_text(
            "defaults:\n  min_agree: 3\nsymbols:\n  NSE:INFY:\n    rsi_period: 21\n",
            encoding="utf-8",
        )
        overrides = SymbolOverrides.load(path, StrategyParams())
        assert overrides.for_symbol("NSE:INFY").rsi_period == 21
        assert overrides.for_symbol("NSE:INFY").min_agree == 3
        assert overrides.for_symbol("NSE:OTHER").min_agree == 3

    def test_alias_overrides_apply_to_the_resolved_symbol(self, tmp_path: Path) -> None:
        path = tmp_path / "overrides.yaml"
        path.write_text("symbols:\n  NFO:NIFTY-FUT:\n    min_agree: 2\n", encoding="utf-8")
        overrides = SymbolOverrides.load(path, StrategyParams())
        aliases = {"NFO:NIFTY-FUT": "NFO:NIFTY26NOVFUT"}
        assert overrides.for_symbol("NFO:NIFTY26NOVFUT", aliases).min_agree == 2

    def test_invalid_override_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "overrides.yaml"
        path.write_text("symbols:\n  NSE:INFY:\n    ema_fast: 40\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            SymbolOverrides.load(path, StrategyParams())

    def test_unknown_top_level_key(self, tmp_path: Path) -> None:
        path = tmp_path / "overrides.yaml"
        path.write_text("symbol:\n  NSE:INFY:\n    min_agree: 2\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown top-level"):
            SymbolOverrides.load(path, StrategyParams())

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="does not exist"):
            SymbolOverrides.load(tmp_path / "nope.yaml", StrategyParams())

    def test_no_file_is_allowed(self) -> None:
        overrides = SymbolOverrides.load(None, StrategyParams())
        assert overrides.for_symbol("NSE:INFY").min_agree == 4
