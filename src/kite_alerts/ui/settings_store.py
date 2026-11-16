"""Atomic, allowlisted writes to ``.env`` with backups.

The settings form edits the same ``.env`` the command line reads. That file is
the user's file, so comments, ordering and unrelated keys survive: the writer
replaces individual assignment lines instead of regenerating the file.

Safety properties (each covered by a test):

* only keys derived from the pydantic settings models may be written;
* the candidate file is validated *before* it replaces the real one, by loading
  it through the same ``load_settings`` the bot uses;
* the write is ``temp file -> fsync -> os.replace`` on the same directory, so an
  interrupted save leaves the original untouched and no half-written file;
* the previous file is copied to ``.env.bak-<UTC timestamp>`` first, and only the
  last five backups are kept (``.env.*`` is already git-ignored);
* a secret value never appears in a result, an error or a log line;
* saves are rate-limited so a stuck finger cannot rewrite the file in a loop.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from ..config.env_file import env_write_lock
from ..config.settings import load_settings
from ..core.errors import ConfigError
from .settings_schema import (
    CONNECT_ONLY_SECRETS,
    ENV_KEYS,
    SECRET_KEYS,
    key_for_path,
)

log = logging.getLogger(__name__)

BACKUP_KEEP = 5
MIN_SAVE_INTERVAL_SECONDS = 1.0
SAVE_RATE_MESSAGE = "Please wait a moment before saving again."
UNKNOWN_KEY_MESSAGE = "The dashboard cannot write {key!r}: it is not a setting it knows about."
CONNECT_ONLY_MESSAGE = "The access token is set by the Connect flow, not here."
MULTILINE_MESSAGE = "{key} must be a single line."

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
_ERROR_LOCATION = re.compile(r"^\s*-\s+([A-Za-z0-9_.]+):\s*(.+)$")


@dataclass(frozen=True, slots=True)
class FieldError:
    key: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "message": self.message}


@dataclass(frozen=True, slots=True)
class SaveResult:
    ok: bool
    message: str
    saved: tuple[str, ...] = ()
    backup: str = ""
    warnings: tuple[str, ...] = ()
    errors: tuple[FieldError, ...] = ()
    retry_after: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "saved": list(self.saved),
            "backup": self.backup,
            "warnings": list(self.warnings),
            "errors": [error.as_dict() for error in self.errors],
            "retry_after": round(self.retry_after, 2),
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    message: str
    warnings: tuple[str, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "warnings": list(self.warnings),
            "summary": self.summary,
        }


def format_env_value(value: Any) -> str:
    """Render one value the way ``python-dotenv`` will read it back."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(item) for item in value)
    text = str(value)
    if text == "":
        return ""
    if re.search(r"[\s#\"']", text) or "=" in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def apply_updates(text: str, updates: Mapping[str, str]) -> str:
    """Replace or append assignment lines, leaving comments and other keys alone."""
    lines = text.splitlines()
    pending = dict(updates)
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        key = match.group(1)
        if key in pending:
            value = pending.pop(key)
            lines[index] = f"{key}={value}"
    for key, value in pending.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def parse_field_errors(message: str) -> tuple[FieldError, ...]:
    """Turn the model's human-readable validation text into per-field errors."""
    errors: list[FieldError] = []
    for line in message.splitlines():
        match = _ERROR_LOCATION.match(line)
        if not match:
            continue
        location, detail = match.group(1), match.group(2).strip()
        key = key_for_path(location.split("."))
        errors.append(FieldError(key=key, message=detail))
    if not errors:
        detail = message.strip().splitlines()[-1]
        # The typed readers raise ``ConfigError("EMA_FAST='x' is not an integer")``
        # without pydantic's location prefix; attach it to the key it names.
        key = next((candidate for candidate in ENV_KEYS if candidate in message), None)
        errors.append(FieldError(key=key, message=detail))
    return tuple(errors)


def _backup_name(now: datetime) -> str:
    return ".env.bak-" + now.strftime("%Y%m%dT%H%M%S%fZ")


def _write_temp(path: Path, text: str) -> None:
    """Write and fsync a temporary sibling so ``os.replace`` is atomic."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass
class SettingsStore:
    """Validated, atomic and rate-limited writer for one ``.env`` file."""

    env_file: Path
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace
    max_backups: int = BACKUP_KEEP
    min_interval: float = MIN_SAVE_INTERVAL_SECONDS
    _last_save: float | None = field(default=None, init=False)

    @property
    def path(self) -> Path:
        return self.env_file

    def read_values(self) -> dict[str, str]:
        if not self.env_file.exists():
            return {}
        try:
            values = dotenv_values(self.env_file)
        except OSError as exc:  # pragma: no cover - unreadable file
            raise ConfigError(f"cannot read {self.env_file}: {exc}") from exc
        return {key: value for key, value in values.items() if value is not None}

    def save(self, values: Mapping[str, Any]) -> SaveResult:
        """Validate then atomically persist ``values`` (a partial update)."""
        now = self.clock()
        if self._last_save is not None:
            remaining = self.min_interval - (now - self._last_save)
            if remaining > 0:
                return SaveResult(
                    ok=False,
                    message=SAVE_RATE_MESSAGE,
                    retry_after=max(0.0, remaining),
                )

        updates: dict[str, str] = {}
        errors: list[FieldError] = []
        for raw_key, raw_value in values.items():
            key = str(raw_key)
            if key not in ENV_KEYS:
                errors.append(FieldError(key, UNKNOWN_KEY_MESSAGE.format(key=key)))
                continue
            if key in CONNECT_ONLY_SECRETS and raw_value not in (None, ""):
                errors.append(FieldError(key, CONNECT_ONLY_MESSAGE))
                continue
            if raw_value is None:
                continue
            rendered = format_env_value(raw_value)
            if "\n" in str(raw_value) or "\r" in str(raw_value):
                errors.append(FieldError(key, MULTILINE_MESSAGE.format(key=key)))
                continue
            updates[key] = rendered
        if errors:
            return SaveResult(ok=False, message="Nothing was saved.", errors=tuple(errors))
        if not updates:
            return SaveResult(ok=True, message="Nothing changed.")

        with env_write_lock(self.env_file):
            existing = self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else ""
            candidate = apply_updates(existing, updates)
            temp = self.env_file.with_name(self.env_file.name + ".tmp")
            temp.parent.mkdir(parents=True, exist_ok=True)
            try:
                _write_temp(temp, candidate)
            except OSError:
                with contextlib.suppress(OSError):
                    temp.unlink()
                return SaveResult(
                    ok=False,
                    message="The settings file could not be written. Nothing was changed.",
                )

            try:
                validated = load_settings(temp, environ={})
            except ConfigError as exc:
                with contextlib.suppress(OSError):
                    temp.unlink()
                field_errors = parse_field_errors(str(exc))
                return SaveResult(
                    ok=False,
                    message="Some values are not valid, so nothing was saved.",
                    errors=field_errors,
                )

            backup = self._make_backup()
            try:
                self.replace(temp, self.env_file)
            except OSError:
                with contextlib.suppress(OSError):
                    temp.unlink()
                log.warning("settings write failed; the original file was kept")
                return SaveResult(
                    ok=False,
                    message="The settings file could not be replaced. The original is unchanged.",
                )

            self._last_save = now

        written = tuple(key for key in updates if key not in SECRET_KEYS)
        message = (
            "Saved. Changes apply the next time the bot starts; the practice replay uses "
            "them immediately."
        )
        if any(key in SECRET_KEYS for key in updates):
            message = "Saved securely. " + message
        return SaveResult(
            ok=True,
            message=message,
            saved=written,
            backup=backup,
            warnings=tuple(validated.warnings_list()),
        )

    def clear_secret(self, key: str) -> SaveResult:
        if key not in SECRET_KEYS:
            return SaveResult(
                ok=False,
                message="That field is not a secret, so it cannot be cleared here.",
                errors=(FieldError(key, "Not a secret field."),),
            )
        return self.save({key: ""})

    def check(self) -> CheckResult:
        """The same validation ``kite-alerts config-check`` performs."""
        try:
            settings = load_settings(self.env_file)
        except ConfigError as exc:
            return CheckResult(
                ok=False,
                message=str(exc),
                warnings=(),
                summary={},
            )
        return CheckResult(
            ok=True,
            message="Your settings are valid.",
            warnings=tuple(settings.warnings_list()),
            summary=settings.redacted(),
        )

    # ------------------------------------------------------------------ backups
    def _make_backup(self) -> str:
        if not self.env_file.exists():
            return ""
        stamp = _backup_name(self.now())
        target = self.env_file.with_name(stamp)
        try:
            shutil.copy2(self.env_file, target)
        except OSError:  # pragma: no cover - disk full / permissions
            log.warning("could not write a settings backup")
            return ""
        self._prune_backups()
        return target.name

    def _prune_backups(self) -> None:
        backups = sorted(self.env_file.parent.glob(self.env_file.name + ".bak-*"))
        for old in backups[: max(0, len(backups) - self.max_backups)]:
            with contextlib.suppress(OSError):
                old.unlink()


__all__ = [
    "BACKUP_KEEP",
    "MIN_SAVE_INTERVAL_SECONDS",
    "CheckResult",
    "FieldError",
    "SaveResult",
    "SettingsStore",
    "apply_updates",
    "format_env_value",
    "parse_field_errors",
]
