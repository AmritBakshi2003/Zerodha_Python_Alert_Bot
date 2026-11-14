"""In-process metrics with Prometheus text rendering (no extra dependency).

Bounded by construction: counters/gauges are keyed by name+labels, and
observations keep only count/sum/max rather than a growing sample list.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

PREFIX = "kite_alerts_"


def _render_labels(labels: Iterable[tuple[str, str]]) -> str:
    items = list(labels)
    if not items:
        return ""
    rendered = ",".join(
        f'{key}="{str(value).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, value in items
    )
    return "{" + rendered + "}"


class Metrics:
    """Thread-safe counters, gauges and simple summaries."""

    def __init__(self, *, max_series: int = 2000) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._summaries: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
        self._max_series = max_series

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._counters and len(self._counters) >= self._max_series:
                return
            self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._gauges and len(self._gauges) >= self._max_series:
                return
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record count/sum/max for ``name`` (a bounded summary, not a histogram)."""
        key = self._key(name, labels)
        with self._lock:
            entry = self._summaries.get(key)
            if entry is None:
                if len(self._summaries) >= self._max_series:
                    return
                entry = [0.0, 0.0, float("-inf")]
                self._summaries[key] = entry
            entry[0] += 1.0
            entry[1] += float(value)
            entry[2] = max(entry[2], float(value))

    def get(self, name: str, **labels: str) -> float:
        key = self._key(name, labels)
        with self._lock:
            if key in self._counters:
                return self._counters[key]
            return self._gauges.get(key, 0.0)

    def snapshot(self) -> dict[str, float]:
        """Flat ``name{labels}`` -> value map (handy for tests and /health)."""
        out: dict[str, float] = {}
        with self._lock:
            for (name, labels), value in self._counters.items():
                out[f"{name}{_render_labels(labels)}"] = value
            for (name, labels), value in self._gauges.items():
                out[f"{name}{_render_labels(labels)}"] = value
            for (name, labels), (count, total, maximum) in self._summaries.items():
                suffix = _render_labels(labels)
                out[f"{name}_count{suffix}"] = count
                out[f"{name}_sum{suffix}"] = total
                out[f"{name}_max{suffix}"] = maximum
        return dict(sorted(out.items()))

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"# TYPE {PREFIX}{name} counter")
                lines.append(f"{PREFIX}{name}{_render_labels(labels)} {value}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {PREFIX}{name} gauge")
                lines.append(f"{PREFIX}{name}{_render_labels(labels)} {value}")
            for (name, labels), (count, total, maximum) in sorted(self._summaries.items()):
                suffix = _render_labels(labels)
                lines.append(f"# TYPE {PREFIX}{name} summary")
                lines.append(f"{PREFIX}{name}_count{suffix} {count}")
                lines.append(f"{PREFIX}{name}_sum{suffix} {total}")
                lines.append(f"{PREFIX}{name}_max{suffix} {maximum}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._summaries.clear()
