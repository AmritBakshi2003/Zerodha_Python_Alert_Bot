"""F16: the manual provider smoke script must fail loudly on a schema change.

``scripts/smoke_sources.py`` is not part of CI (it needs the internet); these
tests pin its pure validation logic with captured provider shapes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_sources.py"


def _load() -> ModuleType:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing (F16)"
    spec = importlib.util.spec_from_file_location("smoke_sources", SCRIPT)
    assert spec is not None, "could not build an import spec"
    assert spec.loader is not None, "import spec has no loader"
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules, so register it before exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _frame(rows: int = 3) -> pd.DataFrame:
    index = pd.date_range("2026-11-16 10:00", periods=rows, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(rows)],
            "High": [101.0 + i for i in range(rows)],
            "Low": [99.0 + i for i in range(rows)],
            "Close": [100.5 + i for i in range(rows)],
            "Volume": [1_000 + i for i in range(rows)],
        },
        index=index,
    )


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def _payload(row_time: int = 1_763_360_400) -> dict[str, Any]:
    return {
        "error": [],
        "result": {
            "XXBTZUSD": [
                [row_time, "100.0", "101.0", "99.0", "100.5", "100.2", "10.0", 5],
                [row_time + 60, "100.5", "102.0", "100.0", "101.5", "101.2", "11.0", 6],
            ],
            "last": row_time + 60,
        },
    }


def test_nse_probe_reports_shape_and_measured_lag() -> None:
    module = _load()
    now = pd.Timestamp("2026-11-16 10:05", tz="UTC").timestamp()

    probe = module.probe_nse(history=lambda symbol, interval: _frame(), now=lambda: now)

    assert probe.provider == "nse_delayed"
    assert probe.badge == "DELAYED"
    assert probe.candles == 3
    assert probe.lag_seconds is not None
    assert probe.lag_seconds >= 0


def test_nse_probe_fails_on_a_changed_schema() -> None:
    module = _load()
    broken = pd.DataFrame({"Close": [100.0, 101.0]})

    # The provider cache wraps the parse error, so the message is generic; what
    # matters is that the probe fails instead of reporting zero candles as ok.
    with pytest.raises(module.SmokeError):
        module.probe_nse(history=lambda symbol, interval: broken)


def test_crypto_probe_reports_shape_and_accepts_the_provider_key() -> None:
    module = _load()
    now = 1_763_360_400 + 120

    probe = module.probe_crypto(
        http_get=lambda *args, **kwargs: _Response(_payload()),
        now=lambda: now,
    )

    assert probe.provider == "crypto_showcase"
    assert probe.badge == "SHOWCASE"
    assert probe.candles == 2
    assert probe.lag_seconds is not None
    assert probe.lag_seconds >= 0


def test_crypto_probe_fails_on_an_error_payload() -> None:
    module = _load()

    with pytest.raises(module.SmokeError):
        module.probe_crypto(
            http_get=lambda *args, **kwargs: _Response(
                {"error": ["EQuery:Unknown asset pair"], "result": {}}
            )
        )


def test_crypto_probe_fails_when_the_row_shape_changes() -> None:
    module = _load()
    payload = _payload()
    payload["result"]["XXBTZUSD"] = [[1_763_360_400, "100.0", "101.0"]]

    with pytest.raises(module.SmokeError, match="candles"):
        module.probe_crypto(http_get=lambda *args, **kwargs: _Response(payload))


def test_main_exits_non_zero_when_a_provider_fails(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load()

    def failing(**_: Any) -> Any:
        raise module.SmokeError("provider returned no candles")

    assert module.main([], nse_probe=failing, crypto_probe=failing) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_zero_when_both_providers_work(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load()
    good = module.Probe(
        provider="nse_delayed",
        symbol="NSE:RELIANCE",
        badge="DELAYED",
        candles=3,
        last_candle_utc=1_763_360_400,
        lag_seconds=12.0,
        stale=False,
        message="",
    )

    assert module.main([], nse_probe=lambda **_: good, crypto_probe=lambda **_: good) == 0
    output = capsys.readouterr().out
    assert "ok" in output
