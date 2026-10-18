import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits = defaultdict(deque)

    def _key(self, request: Request) -> str:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def __call__(self, request: Request) -> None:
        key = self._key(request)
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.max_calls:
            raise HTTPException(status_code=429, detail="Too many requests. Please try again shortly.")
        hits.append(now)