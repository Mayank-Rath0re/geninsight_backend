# core/usage_context.py
"""
Request-scoped collector for LLM token usage.

llm_client calls `record_llm_usage(input_tokens, output_tokens)` after EVERY successful
provider call. services.usage_service.metered_call() opens the collector around a request
and persists the totals when it closes. Uses a ContextVar, so it is safe with FastAPI's
threadpool as long as metered_call() is entered inside the endpoint function itself.
(core/context_propagation.py makes ThreadPoolExecutor workers inherit the context too.)
"""

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UsageCollector:
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    count_query: bool = False  # set True by the endpoint on success (Free-tier query cap)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:  # parallel widget builds may report from several threads
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)
            self.llm_calls += 1

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


_current: ContextVar[Optional[UsageCollector]] = ContextVar("usage_collector", default=None)


def set_collector(c: UsageCollector):
    return _current.set(c)


def reset_collector(token) -> None:
    _current.reset(token)


def record_llm_usage(input_tokens: int, output_tokens: int) -> None:
    """No-op when called outside a metered request (e.g. background jobs)."""
    c = _current.get()
    if c is not None:
        c.add(input_tokens, output_tokens)
