import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, limit: int = 50, window: float = 1.0):
        self._limit = limit
        self._window = window
        self._buckets: dict[str, deque[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        bucket = self._buckets.get(key)

        if bucket is not None:
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]
                bucket = None

        if bucket is None:
            self._buckets[key] = deque([now])
            return True

        if len(bucket) >= self._limit:
            logger.warning("Rate limit exceeded for key %s", key[:16])
            return False

        bucket.append(now)
        return True


rate_limiter = RateLimiter()
