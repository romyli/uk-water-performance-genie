"""A small HTTP client for public publisher endpoints: retries, rate limiting, and full metadata.

It never tries to get past an access control. A browser check or a login page is reported as
`RetrievalBlocked`, and the caller records a manual-bootstrap instruction.
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

LOG = logging.getLogger("water_performance.http")
USER_AGENT = "uk-water-performance-genie/1.0 (+public-data ingestion; contact: deployment operator)"
RETRY_STATUSES = (429, 500, 502, 503, 504)


class RetrievalError(RuntimeError):
    """The publisher did not return the asset after all retries."""


class RetrievalBlocked(RetrievalError):
    """The publisher returned a browser check, login page or similar control instead of the asset."""


@dataclass
class Response:
    url: str  # requested URL
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    retrieved_at: float
    attempts: int = 1

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def metadata(self) -> dict:
        keep = ("content-type", "content-length", "etag", "last-modified", "content-disposition", "date")
        return {
            "requested_url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "headers": {k: v for k, v in self.headers.items() if k in keep},
            "attempts": self.attempts,
        }


Transport = Callable[[str, dict, float], tuple[int, str, dict, bytes]]


def urllib_transport(url: str, headers: dict, timeout: float) -> tuple[int, str, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.geturl() or url, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


@dataclass
class HttpClient:
    transport: Transport = urllib_transport
    max_attempts: int = 5
    backoff_seconds: float = 2.0
    min_interval_seconds: float = 0.2  # at most 5 requests a second to one publisher
    timeout_seconds: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    _last_request: float = field(default=0.0, repr=False)

    def get(self, url: str, accept: str | None = None) -> Response:
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            wait = self.min_interval_seconds - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
            self._last_request = self.clock()
            try:
                status, final_url, resp_headers, body = self.transport(url, headers, self.timeout_seconds)
            except (OSError, TimeoutError) as e:  # network errors are retried
                status, final_url, resp_headers, body = 0, url, {}, b""
                last_error = f"{type(e).__name__}: {e}"
            if 200 <= status < 300:
                resp = Response(url, final_url, status, resp_headers, body, self.clock(), attempt)
                _raise_if_blocked(resp)
                return resp
            if status and status not in RETRY_STATUSES:
                resp = Response(url, final_url, status, resp_headers, body, self.clock(), attempt)
                _raise_if_blocked(resp)
                raise RetrievalError(f"GET {url} returned HTTP {status}")
            last_error = last_error if not status else f"HTTP {status}"
            if attempt < self.max_attempts:
                delay = _retry_after(resp_headers, self.clock()) or self.backoff_seconds * (2 ** (attempt - 1))
                LOG.warning("GET %s failed (%s); retry %d in %.1fs", url, last_error, attempt, delay)
                self.sleep(min(delay, 120.0))
        raise RetrievalError(f"GET {url} failed after {self.max_attempts} attempts: {last_error}")

    def get_json(self, url: str) -> tuple[dict, Response]:
        for attempt in range(1, self.max_attempts + 1):
            resp = self.get(url, accept="application/json")
            try:
                payload = resp.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RetrievalError(f"GET {url} did not return JSON: {e}") from None
            error = payload.get("error") if isinstance(payload, dict) else None
            if not error:
                return payload, resp
            # ArcGIS reports errors, including rate limits, inside a 200 response body.
            if (error or {}).get("code") in RETRY_STATUSES and attempt < self.max_attempts:
                self.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise RetrievalError(f"GET {url} returned ArcGIS error {error}")
        raise RetrievalError(f"GET {url} failed after {self.max_attempts} attempts")


def _retry_after(headers: dict, now: float) -> float | None:
    value = (headers or {}).get("retry-after")
    if not value:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    parsed = email.utils.parsedate_to_datetime(value)
    return max(0.0, parsed.timestamp() - now) if parsed else None


_BLOCK_MARKERS = (
    b"just a moment",
    b"cf-chl",
    b"captcha",
    b"browser verification",
    b"checking your browser",
    b"enable javascript and cookies",
    b"access denied",
)


def _raise_if_blocked(resp: Response) -> None:
    """Spot an interstitial page where a data asset was expected."""
    if resp.status in (401, 403) or (resp.content_type == "text/html" and _looks_like_block(resp.body)):
        raise RetrievalBlocked(
            f"{resp.url} returned HTTP {resp.status} with a browser check or access control page. "
            "Automated retrieval is blocked; use the manual bootstrap route."
        )


def _looks_like_block(body: bytes) -> bool:
    head = body[:20000].lower()
    return any(m in head for m in _BLOCK_MARKERS)
