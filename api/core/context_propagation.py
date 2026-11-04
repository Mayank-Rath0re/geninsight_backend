# core/context_propagation.py
"""
ThreadPoolExecutor.submit() does NOT copy contextvars (asyncio.to_thread and Starlette's
threadpool do). If any service fans LLM calls out through a ThreadPoolExecutor (e.g. building
dashboard widgets in parallel), their token usage would silently escape the meter.
install() patches submit() once so workers run in the submitter's context.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    original = ThreadPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return original(self, ctx.run, fn, *args, **kwargs)

    ThreadPoolExecutor.submit = submit  # type: ignore[assignment]
    _installed = True
