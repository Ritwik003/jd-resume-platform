"""Bounded exponential backoff for transient LLM failures."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from app.services.llm.base import LLMTransientError

logger = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(fn: Callable[[], T], *, max_retries: int = 3, base_delay: float = 1.0) -> T:
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except LLMTransientError as exc:
            last = exc
            if attempt == max_retries:
                break
            delay = base_delay * (2**attempt) + random.uniform(0, 0.4)
            logger.warning("LLM transient failure (attempt %s/%s), retrying in %.1fs", attempt + 1, max_retries, delay)
            time.sleep(delay)
    raise last if last else RuntimeError("retry loop exited unexpectedly")
