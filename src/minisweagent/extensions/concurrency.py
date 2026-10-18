"""Process-wide concurrency control for subagents."""

from __future__ import annotations

import threading
from contextlib import contextmanager


class _Gate:
    def __init__(self, limit: int) -> None:
        self._lock = threading.Lock()
        self._limit = max(1, int(limit))
        self._semaphore = threading.BoundedSemaphore(self._limit)

    @property
    def limit(self) -> int:
        return self._limit

    def configure(self, limit: int) -> None:
        """Change the limit. Call this during startup, not while slots are held."""
        limit = max(1, int(limit))
        with self._lock:
            if limit != self._limit:
                self._limit = limit
                self._semaphore = threading.BoundedSemaphore(limit)

    @contextmanager
    def slot(self, timeout: float | None = None):
        acquired = self._semaphore.acquire(timeout=timeout) if timeout is not None else self._semaphore.acquire()
        if not acquired:
            raise TimeoutError(f"subagent concurrency limit ({self._limit}) reached")
        try:
            yield
        finally:
            self._semaphore.release()


GLOBAL_SUBAGENT_GATE = _Gate(3)


def configure_subagent_concurrency(limit: int) -> None:
    GLOBAL_SUBAGENT_GATE.configure(limit)
