import asyncio

import httpx
import pytest

from ai_radar.config import HostPolicy, Settings
from ai_radar.http import FetchError, HttpClient
from helpers import Router, no_sleep


def make_client(router: Router, *, sleep=no_sleep, clock=None, **settings) -> HttpClient:
    cfg = Settings(**{"retries": 2, "backoff_base": 0.0, **settings})
    kwargs = {"clock": clock} if clock else {}
    return HttpClient(cfg, transport=httpx.MockTransport(router), sleep=sleep, **kwargs)


def run(router: Router, action, **kw):
    async def go():
        async with make_client(router, **kw) as client:
            return await action(client)

    return asyncio.run(go())


def test_retries_server_errors_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, text="ok")

    router = Router().add("example.test", handler=handler)
    assert run(router, lambda c: c.get("https://example.test/x")).text == "ok"
    assert len(calls) == 2


def test_gives_up_after_the_configured_retries() -> None:
    router = Router().add("example.test", status=503)
    with pytest.raises(FetchError, match=r"HTTP 503 from example\.test"):
        run(router, lambda c: c.get("https://example.test/x"))
    assert len(router.requests) == 3  # the first try plus two retries


def test_client_errors_are_not_retried() -> None:
    router = Router().add("example.test", status=404)
    with pytest.raises(FetchError, match="HTTP 404"):
        run(router, lambda c: c.get("https://example.test/missing"))
    assert len(router.requests) == 1


def test_retry_after_is_honoured() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, text="ok")

    router = Router().add("example.test", handler=handler)
    assert run(router, lambda c: c.get("https://example.test/x"), sleep=record).text == "ok"
    assert 3.0 in sleeps


def test_network_errors_become_fetch_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    router = Router().add("example.test", handler=boom)
    with pytest.raises(FetchError, match=r"ConnectError fetching example.test"):
        run(router, lambda c: c.get("https://example.test/x"))
    assert len(router.requests) == 3


def test_invalid_json_is_a_fetch_error() -> None:
    router = Router().add("example.test", text="<html>not json</html>")
    with pytest.raises(FetchError, match="invalid JSON"):
        run(router, lambda c: c.get_json("https://example.test/x"))


def test_rate_limit_responses_say_so() -> None:
    router = Router().add(
        "api.test",
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1794284545"},
    )
    with pytest.raises(FetchError, match="rate limited"):
        run(router, lambda c: c.get("https://api.test/search"))


def test_requests_to_one_host_are_spaced_out_but_other_hosts_are_not() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    router = Router().add("example.test", text="a").add("other.test", text="b")

    async def action(client: HttpClient) -> None:
        for _ in range(3):
            await client.get("https://example.test/x")
        await client.get("https://other.test/x")

    run(
        router,
        action,
        sleep=record,
        clock=lambda: 100.0,  # a frozen clock makes the schedule exact
        hosts={"example.test": HostPolicy(min_interval=1.0, max_concurrency=1)},
    )
    assert sleeps == [1.0, 2.0]


def test_host_policy_can_only_be_tightened() -> None:
    router = Router()
    client = make_client(
        router, hosts={"example.test": HostPolicy(min_interval=3.0, max_concurrency=2)}
    )
    client.set_host_policy("example.test", min_interval=1.0, max_concurrency=5)
    policy = client._policy_for("example.test")
    assert (policy.min_interval, policy.max_concurrency) == (3.0, 2)
    client.set_host_policy("example.test", min_interval=5.0, max_concurrency=1)
    policy = client._policy_for("example.test")
    assert (policy.min_interval, policy.max_concurrency) == (5.0, 1)
    asyncio.run(client.aclose())


def test_robots_txt_is_enforced_cached_and_crawl_delay_is_honoured() -> None:
    router = (
        Router()
        .add("/robots.txt", text="User-agent: *\nDisallow: /private\nCrawl-delay: 2\n")
        .add("example.test", text="page")
    )

    async def action(client: HttpClient):
        assert await client.allowed("https://example.test/public/a") is True
        assert await client.allowed("https://example.test/private/a") is False
        with pytest.raises(FetchError, match="robots"):
            await client.get("https://example.test/private/a", robots=True)
        page = await client.get("https://example.test/public/a", robots=True)
        return page.text, client._policy_for("example.test").min_interval

    text, interval = run(router, action)
    assert text == "page"
    assert interval >= 2.0
    assert len(router.hits("/robots.txt")) == 1  # fetched once per origin


@pytest.mark.parametrize(
    ("robots_status", "allowed"),
    [(404, True), (410, True), (500, False), (503, False), (429, False)],
)
def test_robots_status_codes_follow_rfc_9309(robots_status: int, allowed: bool) -> None:
    router = Router().add("/robots.txt", status=robots_status, text="User-agent: *\nDisallow: /\n")
    assert run(router, lambda c: c.allowed("https://example.test/a")) is allowed


def test_unreachable_robots_txt_means_stay_out() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    router = Router().add("/robots.txt", handler=boom)
    assert run(router, lambda c: c.allowed("https://example.test/a")) is False


def test_robots_can_be_switched_off() -> None:
    router = Router().add("/robots.txt", text="User-agent: *\nDisallow: /\n")
    assert run(router, lambda c: c.allowed("https://example.test/a"), respect_robots=False) is True
    assert router.hits("/robots.txt") == []
