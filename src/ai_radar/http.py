"""Polite async HTTP: per-host pacing, retries with backoff, and robots.txt for HTML pages."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ai_radar.config import HostPolicy, Settings

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 20.0
ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, "
    "application/json;q=0.8, text/html;q=0.7, */*;q=0.5"
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class FetchError(Exception):
    """A request failed in a way worth reporting: HTTP error, timeout, robots.txt block..."""


@dataclass(slots=True)
class _Robots:
    parser: RobotFileParser | None = None
    disallow_all: bool = False

    def can_fetch(self, agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        return True if self.parser is None else self.parser.can_fetch(agent, url)

    def crawl_delay(self, agent: str) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(agent)
        return float(delay) if delay else None


class _HostGate:
    """Caps concurrency per host and spaces out request starts."""

    def __init__(self, policy: HostPolicy, clock: Clock) -> None:
        self.sem = asyncio.Semaphore(policy.max_concurrency)
        self.min_interval = policy.min_interval
        self._clock = clock
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait_turn(self, sleep: Sleep) -> None:
        async with self._lock:
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self.min_interval
        if start > now:
            await sleep(start - now)


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": ACCEPT},
            timeout=httpx.Timeout(settings.timeout, connect=min(10.0, settings.timeout)),
            follow_redirects=True,
            max_redirects=5,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
            transport=transport,
        )
        self._global = asyncio.Semaphore(settings.global_concurrency)
        self._gates: dict[str, _HostGate] = {}
        self._overrides: dict[str, HostPolicy] = {}
        self._robots: dict[str, asyncio.Future[_Robots]] = {}
        self._robots_agent = settings.user_agent.split("/")[0].split()[0]

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- pacing -------------------------------------------------------------------------

    def _policy_for(self, host: str) -> HostPolicy:
        if host in self._overrides:
            return self._overrides[host]
        for pattern, policy in self.settings.hosts.items():
            if host == pattern or host.endswith("." + pattern):
                return policy
        return HostPolicy()

    def set_host_policy(
        self,
        host: str,
        *,
        min_interval: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        """Tighten (never loosen) pacing for a host, e.g. GitHub search without a token."""
        current = self._policy_for(host)
        policy = HostPolicy(
            min_interval=max(current.min_interval, min_interval or 0.0),
            max_concurrency=min(
                current.max_concurrency, max_concurrency or current.max_concurrency
            ),
        )
        self._overrides[host] = policy
        gate = self._gates.get(host)
        if gate is not None:
            gate.min_interval = policy.min_interval

    def _gate(self, host: str) -> _HostGate:
        gate = self._gates.get(host)
        if gate is None:
            gate = self._gates[host] = _HostGate(self._policy_for(host), self._clock)
        return gate

    def _backoff(self, attempt: int) -> float:
        base = self.settings.backoff_base
        return base * (2**attempt) + random.uniform(0, base * 0.5) if base > 0 else 0.0

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("retry-after")
        if not value:
            return None
        if value.isdigit():
            return min(float(value), MAX_RETRY_AFTER)
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return min(max((when - datetime.now(UTC)).total_seconds(), 0.0), MAX_RETRY_AFTER)

    # ---- requests -----------------------------------------------------------------------

    async def _send(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        retries: int,
    ) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        gate = self._gate(host)
        attempt = 0
        while True:
            try:
                async with gate.sem:
                    await gate.wait_turn(self._sleep)
                    async with self._global:
                        resp = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    detail = str(exc) or type(exc).__name__
                    raise FetchError(f"{type(exc).__name__} fetching {host}: {detail}") from exc
                delay = self._backoff(attempt)
            else:
                if resp.status_code not in RETRY_STATUS or attempt >= retries:
                    return resp
                delay = self._retry_after(resp) or self._backoff(attempt)
            log.debug("retrying %s in %.1fs (attempt %d)", url, delay, attempt + 1)
            await self._sleep(delay)
            attempt += 1

    @staticmethod
    def _describe_http_error(resp: httpx.Response) -> str:
        message = f"HTTP {resp.status_code} from {resp.url.host}"
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset", "")
            when = (
                f", resets {datetime.fromtimestamp(int(reset), UTC):%H:%M}Z"
                if reset.isdigit()
                else ""
            )
            message += f" (rate limited{when})"
        return message

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        robots: bool = False,
    ) -> httpx.Response:
        """GET with retries. Raises FetchError on anything but a 2xx/3xx final response."""
        if robots and not await self.allowed(url):
            raise FetchError(f"blocked by robots.txt: {url}")
        resp = await self._send(url, params=params, headers=headers, retries=self.settings.retries)
        if resp.status_code >= 400:
            raise FetchError(self._describe_http_error(resp))
        return resp

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await self.get(url, params=params, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from {resp.url.host}") from exc

    # ---- robots.txt ---------------------------------------------------------------------

    async def allowed(self, url: str) -> bool:
        """May we fetch this HTML page? Follows RFC 9309: 4xx means no rules, 5xx means stay out."""
        if not self.settings.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        pending = self._robots.get(origin)
        if pending is None:
            pending = self._robots[origin] = asyncio.ensure_future(self._load_robots(origin))
        robots = await pending
        delay = robots.crawl_delay(self._robots_agent)
        if delay and parts.hostname:
            self.set_host_policy(parts.hostname, min_interval=min(delay, 10.0))
        return robots.can_fetch(self._robots_agent, url)

    async def _load_robots(self, origin: str) -> _Robots:
        try:
            resp = await self._send(f"{origin}/robots.txt", params=None, headers=None, retries=0)
        except FetchError as exc:
            log.warning("robots.txt unreachable for %s (%s): skipping HTML fetches", origin, exc)
            return _Robots(disallow_all=True)
        if resp.status_code >= 500 or resp.status_code == 429:
            log.warning(
                "robots.txt for %s returned %s: skipping HTML fetches", origin, resp.status_code
            )
            return _Robots(disallow_all=True)
        if resp.status_code >= 400:
            return _Robots()  # no robots.txt: nothing is off limits
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return _Robots(parser=parser)
