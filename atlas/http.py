"""Read-only HTTP client for public market-data endpoints.

Safety: this client can only issue GET requests over HTTPS to an explicit host
allowlist. It has no way to send a request body or authentication headers, so
it cannot place orders or touch an account even if misused.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    """Non-retryable failure (client error, ban, or retries exhausted)."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, float], Response]


def _tls_context() -> ssl.SSLContext:
    """Verified TLS. Uses certifi's CA bundle because some Python builds (e.g. python.org macOS
    installers) ship without system CA access. Certificate verification is never disabled."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_TLS = _tls_context()


def urllib_transport(url: str, timeout: float) -> Response:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "atlas-research/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_TLS) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())


class ReadOnlyHttpClient:
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        allowed_hosts: set[str],
        *,
        min_interval_s: float = 0.25,
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        timeout_s: float = 30.0,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.allowed_hosts = set(allowed_hosts)
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.timeout_s = timeout_s
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self.requests_made = 0

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise HttpError(f"refusing non-HTTPS URL {url!r}")
        if parts.hostname not in self.allowed_hosts:
            raise HttpError(f"host {parts.hostname!r} is not in the market-data allowlist {sorted(self.allowed_hosts)}")

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval_s - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()

    def get_json(self, url: str, params: Mapping[str, object] | None = None) -> tuple[object, Mapping[str, str]]:
        if params:
            url = f"{url}?{urlencode(params)}"
        self._check_url(url)
        attempt = 0
        while True:
            self._throttle()
            self.requests_made += 1
            try:
                resp = self._transport(url, self.timeout_s)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise HttpError(f"TLS certificate verification failed for {url}: {exc}. "
                                    "Not retrying; fix the CA bundle (pip install certifi).") from exc
                reason = f"network error: {exc}"
                retry_after = None
            else:
                if 200 <= resp.status < 300:
                    try:
                        return json.loads(resp.body), resp.headers
                    except json.JSONDecodeError as exc:
                        raise HttpError(f"invalid JSON from {url}: {exc}") from exc
                if resp.status == 418:
                    raise HttpError(f"HTTP 418 (IP ban) from {url}; retry-after={resp.headers.get('retry-after')}. Not retrying.")
                if resp.status not in self.RETRYABLE_STATUS:
                    raise HttpError(f"HTTP {resp.status} from {url}: {resp.body[:300]!r}")
                reason = f"HTTP {resp.status}"
                retry_after = resp.headers.get("retry-after")

            attempt += 1
            if attempt > self.max_retries:
                raise HttpError(f"giving up on {url} after {self.max_retries} retries; last error: {reason}")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = self.backoff_base_s * 2 ** (attempt - 1)
            else:
                delay = self.backoff_base_s * 2 ** (attempt - 1)
            delay = min(delay, self.backoff_max_s)
            log.warning("retrying request", extra={"url": url, "attempt": attempt, "reason": reason, "delay_s": delay})
            self._sleep(delay)
