"""Generic async retry with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

from xiaopaw.observability.metrics import record_external_api_retry

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def async_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    retry_on: tuple[type[Exception], ...] = (Exception,),
    api_name: str | None = None,
    **kwargs: Any,
) -> T:
    """Call ``fn`` asynchronously with retries.

    If ``api_name`` is provided, each retry (except the last attempt) is recorded
    via :func:`xiaopaw.observability.metrics.record_external_api_retry`.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except retry_on as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                if api_name:
                    record_external_api_retry(api_name)
                delay = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "retry %d/%d for %s: %s (backoff %.1fs)",
                    attempt + 1, max_retries, fn.__name__, exc, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
