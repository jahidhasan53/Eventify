"""Sliding-window rate limiter used as a FastAPI dependency.

The default store is in-process memory: correct for a single worker and for tests. When
running several workers/instances, replace `InMemoryRateLimiter` with a shared store (e.g.
Redis) or enforce limits at the reverse proxy as well — see README "Remaining risks".
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import TooManyRequestsError


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False, int(bucket[0] + window_seconds - now) + 1
        bucket.append(now)
        if len(self._hits) > 20_000:  # opportunistic cleanup of idle keys
            for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - window_seconds]:
                self._hits.pop(stale, None)
        return True, 0

    def clear(self) -> None:
        self._hits.clear()


limiter = InMemoryRateLimiter()


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


class RateLimit:
    """Usage: dependencies=[Depends(RateLimit("login", lambda: get_settings().rate_limit_login_per_minute))]"""

    def __init__(self, scope: str, limit: int | Callable[[], int], window_seconds: int = 60) -> None:
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        if not get_settings().rate_limit_enabled:
            return
        limit = self.limit() if callable(self.limit) else self.limit
        allowed, retry_after = limiter.hit(
            f"{self.scope}:{client_ip(request) or 'unknown'}", limit, self.window_seconds
        )
        if not allowed:
            raise TooManyRequestsError(
                "Too many requests. Please try again later.", headers={"Retry-After": str(retry_after)}
            )
