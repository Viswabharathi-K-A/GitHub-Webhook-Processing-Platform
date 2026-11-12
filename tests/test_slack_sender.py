import asyncio
import socket
import time
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

import slack_sender
from slack_sender import SlackSendError, send_slack, validate_slack_url


SLACK_URL = "https://hooks.slack.com/services/T123/B456/test-secret"
REFERENCE = "env:SLACK_WEBHOOK_URL_TENANT_1_DEMO"


@asynccontextmanager
async def local_receiver(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/services/T/B/test-secret"
    finally:
        await runner.cleanup()


def test_secret_reference_is_tenant_scoped(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL_TENANT_1_DEMO", SLACK_URL)
    assert slack_sender.resolve_slack_url(1, REFERENCE) == SLACK_URL
    with pytest.raises(SlackSendError, match="invalid_secret_reference"):
        slack_sender.resolve_slack_url(2, REFERENCE)
    monkeypatch.delenv("SLACK_WEBHOOK_URL_TENANT_1_DEMO")
    with pytest.raises(SlackSendError, match="secret_unavailable"):
        slack_sender.resolve_slack_url(1, REFERENCE)


@pytest.mark.parametrize("url", [
    "http://hooks.slack.com/services/T/B/secret",
    "https://hooks.slack.com.evil.example/services/T/B/secret",
    "https://hooks.slack.com@127.0.0.1/services/T/B/secret",
    "https://user@hooks.slack.com/services/T/B/secret",
    "https://hooks.slack.com:8443/services/T/B/secret",
    "https://127.0.0.1/services/T/B/secret",
    "https://[::1]/services/T/B/secret",
    "https://hooks.slack.com/services/T/B/secret?query=1",
    "https://hooks.slack.com/services/T/B/secret#fragment",
    "https://hooks.slack.com/services/T/B/%2e%2e",
    "https://hooks.slack.com/services/T/B/../secret",
    "https://hooks.slack.com./services/T/B/secret",
    "\n" + SLACK_URL,
    SLACK_URL + "\n",
])
def test_unsafe_urls_are_rejected_without_exposing_url(url):
    with pytest.raises(SlackSendError) as error:
        validate_slack_url(url)
    assert str(error.value) == "invalid_destination"


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "::1", "::ffff:127.0.0.1",
])
def test_connector_rejects_non_public_dns_answers(monkeypatch, address):
    async def resolve(self, host, port, family):
        # Reject the entire answer even if it also contains a public address.
        return [dict(hostname=host, host=ip, port=port, family=socket.AF_INET,
                     proto=0, flags=0) for ip in ("8.8.8.8", address)]

    monkeypatch.setattr(slack_sender.ThreadedResolver, "resolve", resolve)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_TENANT_1_DEMO", SLACK_URL)
    with pytest.raises(SlackSendError, match="non_public_destination"):
        asyncio.run(send_slack(1, REFERENCE, "test"))


def test_connector_uses_validated_address_without_second_dns_lookup(monkeypatch):
    lookups = []
    connections = []

    async def resolve(self, host, port, family):
        lookups.append(host)
        return [dict(hostname=host, host="8.8.8.8", port=port,
                     family=socket.AF_INET, proto=0, flags=0)]

    async def check():
        async def connect(sock, address):
            connections.append(address)
            # Stop before any network traffic; inspect the actual connect address.
            raise OSError("test refusal")

        monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", connect)
        with pytest.raises(SlackSendError, match="http_transport_error"):
            await send_slack(1, REFERENCE, "test")

    monkeypatch.setattr(slack_sender.ThreadedResolver, "resolve", resolve)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_TENANT_1_DEMO", SLACK_URL)
    asyncio.run(check())
    assert lookups == ["hooks.slack.com"]
    assert connections == [("8.8.8.8", 443)]


def test_successful_post_ignores_proxy_environment(monkeypatch):
    received = []

    async def handler(request):
        received.append(await request.json())
        return web.Response(text="ok")

    async def check():
        async with local_receiver(handler) as url:
            # Test-only wiring: production has no flag to allow local URLs.
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            await send_slack(1, REFERENCE, "GitHub push event received.")

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    asyncio.run(check())
    assert received == [{"text": "GitHub push event received.", "mrkdwn": False,
                         "unfurl_links": False, "unfurl_media": False}]


@pytest.mark.parametrize("status, body, reason, retryable", [
    (302, "redirect", "http_302", False),
    (400, "secret response", "http_400", False),
    (403, "secret response", "http_403", False),
    (404, "secret response", "http_404", False),
    (408, "secret response", "http_408", True),
    (429, "secret response", "http_429", True),
    (500, "secret response", "http_500", True),
    (503, "secret response", "http_503", True),
    (200, "not ok", "unexpected_response", False),
])
def test_redirects_and_unsuccessful_responses_are_not_accepted(monkeypatch, status, body, reason, retryable):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response(status=status, text=body, headers={"Location": "/redirected"})

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            with pytest.raises(SlackSendError) as error:
                await send_slack(1, REFERENCE, "test")
            assert str(error.value) == reason
            assert error.value.retryable is retryable

    asyncio.run(check())
    assert paths == ["/services/T/B/test-secret"]


def test_total_timeout_includes_dns(monkeypatch):
    async def resolve(self, host, port, family):
        await asyncio.sleep(1)
        pytest.fail("DNS completed after the request should have timed out")

    monkeypatch.setenv("SLACK_WEBHOOK_URL_TENANT_1_DEMO", SLACK_URL)
    monkeypatch.setattr(slack_sender.ThreadedResolver, "resolve", resolve)
    monkeypatch.setattr(slack_sender, "HTTP_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    with pytest.raises(SlackSendError, match="http_timeout") as error:
        asyncio.run(send_slack(1, REFERENCE, "test"))
    assert error.value.retryable is True
    assert time.monotonic() - started < 0.8


def test_total_timeout_includes_response_body(monkeypatch):
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"o")
        await asyncio.sleep(0.2)
        return response

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            with pytest.raises(SlackSendError, match="http_timeout"):
                await send_slack(1, REFERENCE, "test")

    monkeypatch.setattr(slack_sender, "HTTP_TIMEOUT_SECONDS", 0.05)
    asyncio.run(check())


@pytest.mark.parametrize("header, expected", [
    (None, None), ("120", 120), ("86400", 86400), ("0", 0),
    (" 15 ", 15), ("-1", None), ("1.5", None), ("invalid", None),
])
def test_retry_after_seconds_are_preserved(monkeypatch, header, expected):
    async def handler(request):
        headers = {} if header is None else {"Retry-After": header}
        return web.Response(status=429, headers=headers)

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            with pytest.raises(SlackSendError) as error:
                await send_slack(1, REFERENCE, "test")
            assert error.value.retryable is True
            assert error.value.retry_after == expected
            assert str(error.value) == "http_429"

    asyncio.run(check())


def test_unrepresentable_retry_delay_is_not_shortened(monkeypatch):
    async def handler(request):
        return web.Response(status=429, headers={"Retry-After": "99999999999999999999"})

    async def check():
        async with local_receiver(handler) as url:
            monkeypatch.setattr(slack_sender, "resolve_slack_url", lambda *_: url)
            with pytest.raises(SlackSendError) as error:
                await send_slack(1, REFERENCE, "test")
            assert str(error.value) == "retry_after_out_of_range"
            assert error.value.retryable is False

    asyncio.run(check())
