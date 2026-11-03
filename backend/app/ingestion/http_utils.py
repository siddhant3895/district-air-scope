"""HTTP client utilities with rate-limit pacing, 429 Retry-After / x-ratelimit-reset handling, and exponential backoff."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional
import httpx

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RateLimitedHttpClient:
    """Synchronous HTTP helper enforcing inter-request pacing and retry with exponential backoff."""

    def __init__(
        self,
        *,
        min_interval_sec: float = 1.0,
        max_retries: int = 3,
        base_backoff_sec: float = 1.0,
        timeout_sec: float = 20.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.max_retries = max(0, int(max_retries))
        self.base_backoff_sec = max(0.05, float(base_backoff_sec))
        self.timeout_sec = float(timeout_sec)
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._transport = transport
        self._last_request_ts: Optional[float] = None

    def _pace_before_request(self) -> None:
        if self.min_interval_sec <= 0.0:
            return
        now = self._clock()
        if self._last_request_ts is not None:
            elapsed = now - self._last_request_ts
            if elapsed < self.min_interval_sec:
                self._sleep(self.min_interval_sec - elapsed)
        self._last_request_ts = self._clock()

    def _compute_retry_delay(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After") or response.headers.get("x-ratelimit-reset")
            if retry_after is not None:
                try:
                    return max(0.1, float(retry_after))
                except ValueError:
                    pass
        return self.base_backoff_sec * (2 ** attempt)

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._pace_before_request()
            try:
                with httpx.Client(timeout=self.timeout_sec, transport=self._transport) as client:
                    resp = client.get(url, params=params, headers=headers)
                # Respect proactive rate-limit header when remaining quota hits 0 on a 200 response
                remaining = resp.headers.get("x-ratelimit-remaining")
                reset_sec = resp.headers.get("x-ratelimit-reset")
                if resp.status_code == 200 and remaining is not None and reset_sec is not None:
                    try:
                        if int(remaining) <= 0:
                            self._sleep(max(0.0, float(reset_sec)))
                    except ValueError:
                        pass

                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    delay = self._compute_retry_delay(attempt, resp)
                    self._sleep(delay)
                    continue

                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self._compute_retry_delay(attempt, None))
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                raise

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Failed to fetch {url} after {self.max_retries + 1} attempts.")
