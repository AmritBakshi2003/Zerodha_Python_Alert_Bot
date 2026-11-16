"""Asynchronous notification hub.

Design rules that make a failing channel harmless to the data path:

* ``submit``/``send_text`` only enqueue; the websocket/worker thread never waits
  on the network.
* One worker thread delivers to every channel with bounded retries and jittered
  backoff. A channel that fails is skipped for that message; the others still go.
* Per-channel token buckets respect provider rate limits; a ``429`` waits for the
  provider's own ``retry_after``.
* Duplicate messages (same dedup key within a TTL) are dropped.
* Final failures are written to the dead-letter log and counted - never silent.
* The queue is bounded (``NOTIFY_QUEUE_MAX``, default 5 000). On overflow the
  oldest system message is evicted first, then the oldest alert, so the newest
  alerts survive; ephemeral system messages are dropped outright. Every drop is
  counted (``notifier_dropped_total``) and logged. The policy mirrors the tick
  queue's drop-and-count behaviour in ``engine/worker.py``.
"""

from __future__ import annotations

import datetime
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config.settings import NotifySettings
from ..core.clock import IST
from ..core.models import Alert, Message
from ..core.ports import MetricsPort, gauge, metric
from ..core.resilience import TokenBucket
from .base import DeliveryError, Notifier, RateLimitedError
from .dlq import DeadLetterLog

log = logging.getLogger(__name__)

_STOP = object()


@dataclass
class _DedupEntry:
    expires_at: float


class NotifierHub:
    def __init__(
        self,
        notifiers: Sequence[Notifier],
        *,
        settings: NotifySettings,
        metrics: MetricsPort | None = None,
        dlq: DeadLetterLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: object | None = None,
    ) -> None:
        self._notifiers = list(notifiers)
        self._settings = settings
        self._metrics = metrics
        self._dlq = dlq or DeadLetterLog("logs/dead-letter.jsonl")
        self._sleep = sleep
        self._monotonic = monotonic
        self._queue: queue.Queue[object] = queue.Queue(maxsize=self._settings.queue_max)
        self._thread: threading.Thread | None = None
        self._dedup: dict[str, _DedupEntry] = {}
        self._dedup_lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {
            notifier.name: TokenBucket(max(0.1, settings.max_per_minute / 60.0), sleep=sleep)
            for notifier in self._notifiers
        }
        self._sent = 0
        self._failed = 0
        self._dropped = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def channel_names(self) -> list[str]:
        return [n.name for n in self._notifiers]

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def failed(self) -> int:
        return self._failed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="kite-alerts-notifier", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 20.0) -> None:
        """Flush what is queued (bounded), then stop the worker."""
        if self._thread is None:
            return
        self._offer(_STOP)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.error(
                "notifier did not drain within %.0fs; %d message(s) unsent",
                timeout,
                self._queue.qsize(),
            )
        self._thread = None
        for notifier in self._notifiers:
            try:
                notifier.close()
            except Exception:  # pragma: no cover - shutdown path
                log.debug("notifier %s close failed", notifier.name, exc_info=True)

    # ------------------------------------------------------------------ producers
    @property
    def dropped(self) -> int:
        return self._dropped

    def submit(self, alert: Alert) -> None:
        """Enqueue an alert-derived message (never blocks)."""
        message = Message(
            text=alert.message(),
            level="alert",
            subject=f"[kite-alerts] {alert.side.value} {alert.symbol} @ {alert.price:.2f}",
            dedup_key=alert.dedup_key,
            meta={"symbol": alert.symbol, "side": alert.side.value, "ts": alert.ts.isoformat()},
        )
        self._enqueue(message)

    def send_text(
        self,
        text: str,
        *,
        level: str = "info",
        dedup_key: str | None = None,
        subject: str = "",
    ) -> None:
        self._enqueue(Message(text=text, level=level, dedup_key=dedup_key, subject=subject))

    def _enqueue(self, message: Message) -> None:
        if not self._notifiers:
            log.warning("no notifier configured; dropping message: %s", message.text[:120])
            return
        if message.dedup_key and not self._dedup_ok(message.dedup_key):
            metric(self._metrics, "notify_duplicates")
            log.debug("duplicate notification dropped: %s", message.dedup_key)
            return
        self._offer(message)
        gauge(self._metrics, "notify_queue_depth", float(self._queue.qsize()))

    def _offer(self, item: object) -> None:
        """Enqueue ``item`` (a Message or the ``_STOP`` sentinel) without blocking.

        The queue is bounded (``NOTIFY_QUEUE_MAX``). On overflow:
        * a ``system``/``info`` message is dropped immediately (ephemeral);
        * an ``alert`` evicts the oldest system-level message if one exists,
          otherwise the oldest alert, so the newest alerts survive;
        * the ``_STOP`` sentinel is always enqueued (evicting one message if
          needed) so ``close()`` cannot deadlock on a full queue.

        Every drop is counted (``notifier_dropped_total``) and logged.
        """
        q = self._queue
        while True:
            try:
                q.put_nowait(item)
                return
            except queue.Full:
                if not self._evict_one(item):
                    self._count_drop(item if isinstance(item, Message) else None)
                    return

    def _evict_one(self, item: object) -> bool:
        """Remove one victim from the full queue to make room. Return True on success."""
        q = self._queue
        with q.mutex:
            entries = q.queue  # underlying deque, guarded by q.mutex
            if not entries:
                return False
            # Prefer evicting the oldest system/info message (never _STOP).
            victim_index: int | None = None
            for index, queued in enumerate(entries):
                if queued is _STOP:
                    continue
                if isinstance(queued, Message) and queued.level != "alert":
                    victim_index = index
                    break
            if victim_index is None:
                # No system message: evict the oldest non-_STOP entry.
                for index, queued in enumerate(entries):
                    if queued is not _STOP:
                        victim_index = index
                        break
            if victim_index is None:
                # Queue holds only _STOP (shouldn't happen); drop incoming instead.
                return False
            victim = entries[victim_index]
            del entries[victim_index]
            # The victim was never gotten, so task_done() will never be called.
            q.unfinished_tasks = max(0, q.unfinished_tasks - 1)
            if isinstance(victim, Message):
                self._dropped += 1
                metric(self._metrics, "notifier_dropped_total")
                log.warning(
                    "notifier queue full (%d); dropped oldest %s message",
                    self._settings.queue_max,
                    victim.level,
                )
            return True

    def _count_drop(self, message: Message | None) -> None:
        self._dropped += 1
        level = message.level if message is not None else "unknown"
        metric(self._metrics, "notifier_dropped_total")
        log.warning(
            "notifier queue full (%d); dropped incoming %s message", self._settings.queue_max, level
        )

    def _dedup_ok(self, key: str) -> bool:
        ttl = self._settings.dedup_seconds
        if ttl <= 0:
            return True
        now = self._monotonic()
        with self._dedup_lock:
            if len(self._dedup) > 1000:
                self._dedup = {k: v for k, v in self._dedup.items() if v.expires_at > now}
            entry = self._dedup.get(key)
            if entry is not None and entry.expires_at > now:
                return False
            self._dedup[key] = _DedupEntry(expires_at=now + ttl)
            return True

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, Message)
                self._deliver(item)
            finally:
                self._queue.task_done()
                gauge(self._metrics, "notify_queue_depth", float(self._queue.qsize()))

    def _deliver(self, message: Message) -> None:
        channels_ok: list[str] = []
        for notifier in self._notifiers:
            if self._deliver_one(notifier, message):
                channels_ok.append(notifier.name)
            elif message.level == "alert":
                metric(self._metrics, "alerts_failed")
        if channels_ok:
            self._sent += 1
            metric(
                self._metrics, "alerts_sent" if message.level == "alert" else "notifications_sent"
            )
        log.debug("delivered %s via %s", message.level, channels_ok or "nothing")

    def _deliver_one(self, notifier: Notifier, message: Message) -> bool:
        attempts = 0
        max_attempts = self._settings.retries
        while attempts < max_attempts:
            attempts += 1
            try:
                bucket = self._buckets.get(notifier.name)
                if bucket is not None:
                    bucket.acquire()
                notifier.send(message)
                return True
            except RateLimitedError as exc:
                wait = max(exc.retry_after, 1.0)
                metric(self._metrics, "notify_rate_limited", 1, channel=notifier.name)
                log.warning("%s rate limited; waiting %.1fs", notifier.name, wait)
                if attempts >= max_attempts:
                    self._dead_letter(notifier, message, exc, attempts)
                    return False
                self._sleep(wait)
            except DeliveryError as exc:
                delay = min(30.0, 0.5 * (2 ** (attempts - 1)))
                log.warning(
                    "%s delivery attempt %d/%d failed: %s",
                    notifier.name,
                    attempts,
                    max_attempts,
                    exc.detail,
                )
                if attempts >= max_attempts:
                    self._dead_letter(notifier, message, exc, attempts)
                    return False
                self._sleep(delay)
            except Exception as exc:
                log.exception("%s raised an unexpected error", notifier.name)
                self._dead_letter(notifier, message, exc, attempts)
                return False
        return False

    def _dead_letter(
        self, notifier: Notifier, message: Message, exc: BaseException, attempts: int
    ) -> None:
        self._failed += 1
        metric(self._metrics, "notify_dead_letters", 1, channel=notifier.name)
        self._dlq.record(channel=notifier.name, message=message, error=exc, attempts=attempts)
        log.error(
            "alert lost by %s after %d attempt(s); preserved in %s: %s",
            notifier.name,
            attempts,
            self._dlq.path,
            message.text[:160],
        )

    # ------------------------------------------------------------------ helpers
    def flush(self, timeout: float = 10.0) -> bool:
        """Wait for the queue to drain (used by tests and graceful shutdown)."""
        deadline = self._monotonic() + timeout
        while self._queue.unfinished_tasks and self._monotonic() < deadline:
            self._sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def health(self) -> dict[str, object]:
        return {
            "channels": self.channel_names,
            "queue_depth": self.queue_depth,
            "sent": self._sent,
            "failed": self._failed,
            "dead_letters": self._dlq.count,
            "dropped": self._dropped,
            "now": datetime.datetime.now(tz=IST).isoformat(),
        }
