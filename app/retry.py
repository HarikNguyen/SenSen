"""Shared transient-error detection for external API calls, used by
app/ocr_api.py and app/deep_scan.py. Callers wrapping a provider exception
(e.g. langextract's InferenceRuntimeError) must unwrap `.original` first.
"""

import logging
import time
from typing import Callable, TypeVar

import openai
from google.genai import errors as genai_errors

T = TypeVar("T")

logger = logging.getLogger("sensen.retry")

# 429/5xx: transient, worth a backoff retry (unlike a one-off client bug).
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (3, 10)  # sleep before attempt 2, then attempt 3


def is_transient_error(exc: Exception) -> bool:
    """True for a 429/5xx from OpenAI (also covers Grok) or google-genai."""
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, genai_errors.APIError):
        return exc.code in RETRYABLE_STATUS_CODES
    return False


def call_with_backoff(fn: Callable[[], T], *, label: str) -> T:
    """Retries `fn` up to MAX_ATTEMPTS on a transient error; anything else propagates."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_transient_error(exc) or attempt == MAX_ATTEMPTS:
                raise
            delay = BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                "%s: transient error (%s), backing off %ds before retry %d/%d",
                label, exc, delay, attempt + 1, MAX_ATTEMPTS,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises above
