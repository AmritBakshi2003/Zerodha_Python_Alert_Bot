"""Settings page: metadata, validation, atomic writes, backups and secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from kite_alerts.config.settings import load_settings
from kite_alerts.ui.settings_schema import (
    ALL_SPECS,
    ENV_KEYS,
    SETTINGS_GROUPS,
    SPEC_BY_ENV,
    field_metas,
    key_for_path,
    settings_payload,
)
from kite_alerts.ui.settings_store import (
    CONNECT_ONLY_MESSAGE as _CONNECT_ONLY,
)
from kite_alerts.ui.settings_store import (
    SettingsStore,
    apply_updates,
    format_env_value,
    parse_field_errors,
)
from unit.ui_support import FakeMonotonic, make_client
from unit.ui_support_c2 import (
    BASE_ENV,
    KEY_MARKER,
    SECRET_MARKER,
    SLACK_MARKER,
    TELEGRAM_MARKER,
    client_for,
    make_c2_context,
    write_env,
)


@pytest.fixture
def ui(tmp_path: Path):
    client, context, env_path = client_for(tmp_path)
    with client:
        yield client, context, env_path


def field_by_key(payload: dict, key: str) -> dict:
    for group in payload["groups"]:
        for field in group["fields"]:
            if field["key"] == key:
                return field
    raise AssertionError(f"{key} not in payload")


class TestSchema:
    def test_every_env_key_is_consumed_by_the_loader(self, tmp_path: Path) -> None:
        lines = []
        for meta in field_metas(None):
            value = format_env_value(meta.default)
            lines.append(f"{meta.spec.env}={value}")
        env = write_env(tmp_path, "\n".join(lines) + "\n")
        settings = load_settings(env, environ={})
        assert settings.env_warnings == ()
        assert {meta.spec.env for meta in field_metas(None)} == set(ENV_KEYS)

    def test_schema_reports_defaults_bounds_and_choices(self) -> None:
        metas = {meta.spec.env: meta for meta in field_metas(None)}
        assert metas["EMA_FAST"].default == 9
        assert (metas["EMA_FAST"].minimum, metas["EMA_FAST"].maximum) == (1, 500)
        assert metas["EMA_FAST"].kind == "int"
        assert metas["INDICATOR_SEED"].choices == ("first", "sma")
        assert metas["NOTIFY_CHANNELS"].kind == "multi"
        assert metas["SYMBOLS"].kind == "list"
        assert metas["TELEGRAM_BOT_TOKEN"].secret is True
        assert metas["KITE_ACCESS_TOKEN"].spec.editable is False

    def test_groups_match_the_page(self) -> None:
        ids = [group[0] for group in SETTINGS_GROUPS]
        assert ids == ["instruments", "rules", "notifications", "advanced"]

    def test_paths_map_back_to_keys(self) -> None:
        assert key_for_path(("params", "ema_fast")) == "EMA_FAST"
        assert key_for_path(("params", "rules", "rsi_zone", "weight")) == ("RULE_RSI_ZONE_WEIGHT")
        assert key_for_path(("notify", "channels")) == "NOTIFY_CHANNELS"
        assert key_for_path(("nonsense",)) is None

    def test_non_secret_values_are_present_but_secrets_are_not(self, ui) -> None:
        client, _context, env_path = ui
        response = client.get("/api/settings")
        assert response.status_code == 200
        payload = response.json()
        assert [group["title"] for group in payload["groups"]] == [
            "Instruments",
            "Alert rules",
            "Notifications",
            "Advanced",
        ]
        assert field_by_key(payload, "EMA_FAST")["value"] == 9
        telegram = field_by_key(payload, "TELEGRAM_BOT_TOKEN")
        assert "value" not in telegram
        assert telegram["secret"] is True
        assert telegram["is_set"] is True
        assert payload["connect"]["fields"][0]["is_set"] is True
        for marker in (KEY_MARKER, SECRET_MARKER, TELEGRAM_MARKER, SLACK_MARKER):
            assert marker not in response.text
        assert KEY_MARKER in env_path.read_text(encoding="utf-8")

    def test_payload_without_settings_is_safe(self) -> None:
        payload = settings_payload(None, settings_error="bad", env_file=".env")
        for group in payload["groups"]:
            for field in group["fields"]:
                if field["secret"]:
                    assert "value" not in field
                    assert field["is_set"] is False


class TestSave:
    def test_save_updates_the_file_and_keeps_comments(self, ui) -> None:
        client, context, env_path = ui
        response = client.put("/api/settings", json={"values": {"EMA_FAST": 12}})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["saved"] == ["EMA_FAST"]
        text = env_path.read_text(encoding="utf-8")
        assert "# a comment the writer must preserve" in text
        assert "KITE_API_KEY=KEY-MARKER-4f1a" in text
        assert "EMA_FAST=12" in text
        assert body["backup"].startswith(".env.bak-")
        assert (env_path.parent / body["backup"]).exists()
        assert not (env_path.parent / ".env.tmp").exists()
        assert load_settings(env_path, environ={}).params.ema_fast == 12
        assert context.settings is not None
        assert context.settings.params.ema_fast == 12
        assert field_by_key(client.get("/api/settings").json(), "EMA_FAST")["value"] == 12

    def test_invalid_value_is_reported_per_field_and_nothing_is_written(self, ui) -> None:
        client, _context, env_path = ui
        before = env_path.read_text(encoding="utf-8")
        response = client.put("/api/settings", json={"values": {"EMA_FAST": 5000}})
        assert response.status_code == 422
        body = response.json()
        assert body["ok"] is False
        assert body["errors"][0]["key"] == "EMA_FAST"
        assert "less than or equal to 500" in body["errors"][0]["message"]
        assert env_path.read_text(encoding="utf-8") == before

    def test_wrong_type_is_attached_to_its_field(self, ui) -> None:
        client, _context, _env = ui
        response = client.put("/api/settings", json={"values": {"EMA_FAST": "abc"}})
        assert response.status_code == 422
        error = response.json()["errors"][0]
        assert error["key"] == "EMA_FAST"
        assert "not an integer" in error["message"]

    def test_unknown_key_is_refused(self, ui) -> None:
        client, _context, _env = ui
        response = client.put("/api/settings", json={"values": {"EVIL_KEY": "1"}})
        assert response.status_code == 422
        assert response.json()["errors"][0]["key"] == "EVIL_KEY"
        assert "not a setting" in response.json()["errors"][0]["message"]

    def test_access_token_cannot_be_set_here(self, ui) -> None:
        client, _context, _env = ui
        response = client.put("/api/settings", json={"values": {"KITE_ACCESS_TOKEN": "abc"}})
        assert response.status_code == 422
        assert "Connect flow" in response.json()["errors"][0]["message"]

    def test_multiline_value_is_refused(self, ui) -> None:
        client, _context, _env = ui
        response = client.put("/api/settings", json={"values": {"SYMBOLS": "A\nB"}})
        assert response.status_code == 422
        assert response.json()["errors"][0]["key"] == "SYMBOLS"

    def test_unknown_keys_already_in_the_file_are_preserved(self, ui) -> None:
        client, _context, env_path = ui
        env_path.write_text(BASE_ENV + "MY_OWN_NOTE=keep me\n", encoding="utf-8")
        response = client.put("/api/settings", json={"values": {"EMA_SLOW": 30}})
        assert response.status_code == 200
        text = env_path.read_text(encoding="utf-8")
        assert "MY_OWN_NOTE=keep me" in text
        assert "EMA_SLOW=30" in text

    def test_saves_are_rate_limited(self, tmp_path: Path) -> None:
        clock = FakeMonotonic()
        context, _env = make_c2_context(tmp_path, fast_saves=False)
        context.settings_store.clock = clock
        context.settings_store.min_interval = 1.0
        with make_client(context) as client:
            first = client.put("/api/settings", json={"values": {"EMA_FAST": 10}})
            assert first.status_code == 200
            second = client.put("/api/settings", json={"values": {"EMA_FAST": 11}})
            assert second.status_code == 429
            assert second.headers["retry-after"] == "1"
            clock.advance(1.0)
            third = client.put("/api/settings", json={"values": {"EMA_FAST": 11}})
            assert third.status_code == 200

    def test_interrupted_replace_keeps_the_original(self, ui, monkeypatch) -> None:
        client, context, env_path = ui
        before = env_path.read_text(encoding="utf-8")

        def explode(_src, _dst) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(context.settings_store, "replace", explode)
        response = client.put("/api/settings", json={"values": {"EMA_SLOW": 30}})
        assert response.status_code == 400
        assert "original is unchanged" in response.json()["message"]
        assert env_path.read_text(encoding="utf-8") == before
        assert not (env_path.parent / ".env.tmp").exists()

    def test_only_the_last_five_backups_are_kept(self, ui) -> None:
        client, _context, env_path = ui
        for value in range(7):
            response = client.put("/api/settings", json={"values": {"EMA_FAST": 9 + value}})
            assert response.status_code == 200
        backups = sorted(env_path.parent.glob(".env.bak-*"))
        assert len(backups) == 5


class TestSecrets:
    def test_clearing_a_secret_updates_the_set_indicator(self, ui) -> None:
        client, _context, env_path = ui
        response = client.put("/api/settings", json={"values": {"TELEGRAM_BOT_TOKEN": ""}})
        assert response.status_code == 200
        assert TELEGRAM_MARKER not in response.text
        assert "TELEGRAM_BOT_TOKEN=\n" in env_path.read_text(encoding="utf-8")
        payload = client.get("/api/settings").json()
        telegram = field_by_key(payload, "TELEGRAM_BOT_TOKEN")
        assert telegram["is_set"] is False
        assert "value" not in telegram

    def test_clear_secret_refuses_non_secret_keys(self, tmp_path: Path) -> None:
        store = SettingsStore(tmp_path / ".env")
        result = store.clear_secret("EMA_FAST")
        assert result.ok is False
        assert result.errors[0].key == "EMA_FAST"

    def test_a_bad_secret_is_never_echoed(self, ui) -> None:
        client, _context, _env = ui
        response = client.put(
            "/api/settings",
            json={
                "values": {
                    "NOTIFY_CHANNELS": ["telegram"],
                    "TELEGRAM_BOT_TOKEN": "DO-NOT-ECHO-MARKER",
                }
            },
        )
        assert response.status_code == 422
        assert "DO-NOT-ECHO-MARKER" not in response.text

    def test_check_settings_is_redacted(self, ui) -> None:
        client, _context, _env = ui
        response = client.post("/api/settings/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["summary"]["api_secret_present"] is True
        for marker in (SECRET_MARKER, TELEGRAM_MARKER, SLACK_MARKER, KEY_MARKER):
            assert marker not in response.text

    def test_check_settings_reports_a_broken_file(self, ui) -> None:
        client, context, env_path = ui
        env_path.write_text(BASE_ENV + "EMA_FAST=not-a-number\n", encoding="utf-8")
        context.reload()
        response = client.post("/api/settings/check")
        assert response.status_code == 422
        assert response.json()["ok"] is False
        assert "EMA_FAST" in response.json()["message"]


class TestWriterHelpers:
    def test_apply_updates_replaces_and_appends(self) -> None:
        text = "# keep\nA=1\n\nB=2\n"
        result = apply_updates(text, {"A": "9", "C": "3"})
        assert result == "# keep\nA=9\n\nB=2\nC=3\n"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "true"),
            (False, "false"),
            (12, "12"),
            (["NSE:A", "NSE:B"], "NSE:A,NSE:B"),
            ("plain", "plain"),
            ("with space", '"with space"'),
            ("has#hash", '"has#hash"'),
            ("a=b", '"a=b"'),
            ("", ""),
        ],
    )
    def test_format_env_value(self, value, expected: str) -> None:
        assert format_env_value(value) == expected

    def test_parse_field_errors_uses_locations_and_names(self) -> None:
        errors = parse_field_errors(
            "invalid configuration:\n"
            "  - params.ema_fast: Input should be less than or equal to 500\n"
            "  - notify: TELEGRAM_CHAT_ID is required\n"
        )
        assert errors[0].key == "EMA_FAST"
        assert errors[1].key is None
        generic = parse_field_errors("invalid configuration: EMA_FAST='x' is not an integer")
        assert generic[0].key == "EMA_FAST"

    def test_connect_only_message_is_actionable(self) -> None:
        assert "Connect flow" in _CONNECT_ONLY

    def test_specs_all_belong_to_a_known_group(self) -> None:
        groups = {group[0] for group in SETTINGS_GROUPS} | {"connect"}
        assert all(spec.group in groups for spec in ALL_SPECS)
        assert SPEC_BY_ENV["TELEGRAM_BOT_TOKEN"].group == "notifications"


class TestNoTmpInMessages:
    def test_no_settings_message_contains_tmp(self, ui, monkeypatch: pytest.MonkeyPatch) -> None:
        client, context, _env_path = ui
        store = context.settings_store
        assert store is not None

        r = client.put("/api/settings", json={"values": {"WARMUP_BARS": 120}})
        store.min_interval = 0.0
        store._last_save = store.clock()
        r_rate = client.put("/api/settings", json={"values": {"WARMUP_BARS": 121}})
        r_bad = client.put("/api/settings", json={"values": {"WARMUP_BARS": 0}})
        r_unk = client.put("/api/settings", json={"values": {"BOGUS_KEY": 1}})
        r_ml = client.put("/api/settings", json={"values": {"SYMBOLS": "NSE:A\nNSE:B"}})
        r_sec = client.put("/api/settings", json={"values": {"KITE_ACCESS_TOKEN": "x"}})
        r_check = client.post("/api/settings/check")

        original_replace = store.replace

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        store.replace = boom  # type: ignore[assignment]
        store.min_interval = 0.0
        store._last_save = None
        r_fail = client.put("/api/settings", json={"values": {"WARMUP_BARS": 122}})
        store.replace = original_replace

        for resp in (r, r_rate, r_bad, r_unk, r_ml, r_sec, r_check, r_fail):
            body = resp.json()
            texts = [body.get("message", "")]
            texts.extend(err.get("message", "") for err in body.get("errors", []))
            for text in texts:
                assert ".tmp" not in text, text
