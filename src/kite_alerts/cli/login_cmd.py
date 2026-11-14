"""Daily Kite access-token refresh.

The prototype ran this at import time, needed a ``.env`` to already exist and
failed if you pasted the whole redirect URL. Here it is a subcommand, accepts
either the token or the full ``request_token`` URL, writes the file atomically
with ``0600`` permissions, and can print the token for a secret manager instead.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ..config.settings import load_settings
from ..core.errors import ConfigError

_TOKEN_RE = re.compile(r"request_token=([A-Za-z0-9]+)")


def extract_request_token(value: str) -> str:
    """Accept a bare token or the full redirect URL."""
    value = value.strip()
    match = _TOKEN_RE.search(value)
    return match.group(1) if match else value


def write_access_token(env_path: Path, token: str) -> None:
    """Update ``KITE_ACCESS_TOKEN`` in place, preserving comments and other keys."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("KITE_ACCESS_TOKEN="):
            lines[index] = f"KITE_ACCESS_TOKEN={token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"KITE_ACCESS_TOKEN={token}")

    temp = env_path.with_suffix(env_path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    temp.replace(env_path)


def login(
    *,
    env_file: str | Path = ".env",
    api_key: str | None = None,
    api_secret: str | None = None,
    request_token: str | None = None,
    print_only: bool = False,
    input_fn: Callable[[str], str] = input,
) -> int:
    settings = load_settings(env_file)
    key = api_key or settings.kite.api_key
    secret = api_secret or settings.kite.api_secret.get_secret_value()
    if not key or not secret:
        raise ConfigError(
            "KITE_API_KEY and KITE_API_SECRET are required for login " f"(set them in {env_file})"
        )

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=key)
    print("1) Open this URL, log in with your Zerodha account and approve:")
    print(f"   {kite.login_url()}")
    print("2) Copy the `request_token` value (or paste the whole redirected URL).")

    raw = request_token if request_token is not None else input_fn("request_token: ")
    token = extract_request_token(raw)
    if not token:
        raise ConfigError("no request_token supplied")

    data = kite.generate_session(token, api_secret=secret)
    access_token = str(data["access_token"])

    if print_only:
        print(access_token)
        return 0

    path = Path(env_file)
    write_access_token(path, access_token)
    print(f"Saved KITE_ACCESS_TOKEN to {path} (valid until ~06:00 IST tomorrow).")
    print("Restart the bot: `kite-alerts run`.")
    return 0


def login_main(args: object) -> int:
    ns = vars(args) if not isinstance(args, dict) else args
    try:
        return login(
            env_file=ns.get("env_file") or ".env",
            api_key=ns.get("api_key"),
            api_secret=ns.get("api_secret"),
            request_token=ns.get("request_token"),
            print_only=bool(ns.get("stdout")),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
