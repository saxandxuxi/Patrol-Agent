from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import settings


class SharedRateLimiter:
    """Space requests for one upstream identity across all worker threads."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_available: dict[str, float] = {}

    def acquire(self, key: str, rpm: int) -> None:
        if rpm <= 0:
            return
        interval = 60.0 / rpm
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_available.get(key, now))
            self._next_available[key] = scheduled + interval
        wait = scheduled - now
        if wait > 0:
            self._sleep(wait)

    def defer(self, key: str, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        with self._lock:
            until = self._clock() + delay
            self._next_available[key] = max(
                self._next_available.get(key, 0.0),
                until,
            )


def model_provider(model: dict[str, Any]) -> str:
    provider = str(model.get("provider") or "").strip().lower()
    if provider:
        return provider
    base_url = str(model.get("base_url") or "").rstrip("/")
    for name, config in settings.provider_configs().items():
        if str(config.get("base_url") or "").rstrip("/") == base_url:
            return name
    return ""


def model_request_key(model: dict[str, Any]) -> str:
    return "|".join(
        (
            model_provider(model),
            str(model.get("base_url") or "").rstrip("/"),
            str(model.get("model") or ""),
        )
    )


def requests_per_minute(model: dict[str, Any]) -> int:
    provider = model_provider(model)
    model_name = str(model.get("model") or "").strip()
    wanted = f"{provider}/{model_name}"
    for item in str(settings.llm_model_rate_limits or "").split(","):
        name, separator, value = item.strip().partition("=")
        if not separator or name.strip().lower() != wanted.lower():
            continue
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


shared_rate_limiter = SharedRateLimiter()
