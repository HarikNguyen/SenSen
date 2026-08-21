"""Proactive sliding-window rate limiting -- waits for a free slot before
calling, instead of only reacting to a 429 (see app/retry.py). One shared
`gemini_limiter` instance for app/ocr_api.py and app/deep_scan.py, since
both hit the same Gemini RPM budget. RPM only, not RPD -- daily quota is
app/pages.py's per-key lifetime caps instead.
"""

import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger("sensen.rate_limiter")

_WINDOW_SECONDS = 60.0


class SlidingWindowRateLimiter:
    """Thread-safe: blocks until a call is safe within a rolling 60s window."""

    def __init__(self, max_calls: int, label: str):
        self.max_calls = max_calls
        self.label = label
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """max_calls <= 0 disables throttling."""
        if self.max_calls <= 0:
            return
        with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and self._calls[0] <= now - _WINDOW_SECONDS:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait_for = self._calls[0] + _WINDOW_SECONDS - now + 0.05
                logger.warning(
                    "%s: at %d/%d calls in the last %ds, waiting %.1fs for a free slot",
                    self.label, len(self._calls), self.max_calls, _WINDOW_SECONDS, wait_for,
                )
                time.sleep(wait_for)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r isn't a valid integer, using default %d", name, raw, default)
        return default


# Conservative defaults -- real RPM varies by account/tier and changes
# over time; override via env var. 0 disables throttling.
GEMINI_RPM_LIMIT = _int_env("GEMINI_RPM_LIMIT", 10)
OPENAI_RPM_LIMIT = _int_env("OPENAI_RPM_LIMIT", 60)
XAI_RPM_LIMIT = _int_env("XAI_RPM_LIMIT", 60)

# Shared across app/ocr_api.py and app/deep_scan.py so they compete for the same budget.
gemini_limiter = SlidingWindowRateLimiter(GEMINI_RPM_LIMIT, label="gemini")
openai_limiter = SlidingWindowRateLimiter(OPENAI_RPM_LIMIT, label="openai")
xai_limiter = SlidingWindowRateLimiter(XAI_RPM_LIMIT, label="grok")
