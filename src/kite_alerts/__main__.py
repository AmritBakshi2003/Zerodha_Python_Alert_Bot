"""``python -m kite_alerts`` -> the CLI."""

from __future__ import annotations

from .cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
