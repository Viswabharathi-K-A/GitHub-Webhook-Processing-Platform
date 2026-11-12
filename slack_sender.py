import asyncio
import ipaddress
import os
import re
import socket

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver


HTTP_TIMEOUT_SECONDS = 10


class SlackSendError(Exception):
    """Contains only a sanitized category, never a URL or response body."""

    def __init__(self, reason, *, retryable=False, retry_after=None):
        super().__init__(reason)
        self.retryable = retryable
        self.retry_after = retry_after


def resolve_slack_url(tenant_id, reference):
    prefix = f"env:SLACK_WEBHOOK_URL_TENANT_{tenant_id}_"
    if not isinstance(reference, str) or not re.fullmatch(
        re.escape(prefix) + r"[A-Z0-9_]+", reference
    ):
        raise SlackSendError("invalid_secret_reference")
    url = os.environ.get(reference.removeprefix("env:"))
    if not url:
        raise SlackSendError("secret_unavailable")
    validate_slack_url(url)
    return url


def validate_slack_url(url):
    # Match the entire URL, avoiding parser normalization of whitespace or escapes.
    if not isinstance(url, str) or len(url) > 2048 or not re.fullmatch(
        r"https://hooks\.slack\.com(?::443)?/services/"
        r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", url
    ):
        raise SlackSendError("invalid_destination")


class PublicSlackResolver(AbstractResolver):
    """Pass only public IPv4 addresses directly to aiohttp's connector."""

    def __init__(self):
        self.resolver = ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != "hooks.slack.com" or port != 443:
            raise SlackSendError("invalid_destination")
        addresses = await self.resolver.resolve(host, port, family=socket.AF_INET)
        if not addresses:
            raise SlackSendError("dns_unavailable", retryable=True)
        for address in addresses:
            ip = ipaddress.ip_address(address["host"])
            if ip.version != 4 or not ip.is_global or ip.is_multicast:
                raise SlackSendError("non_public_destination")
        return addresses

    async def close(self):
        await self.resolver.close()


async def send_slack(tenant_id, reference, text):
    url = resolve_slack_url(tenant_id, reference)
    resolver = PublicSlackResolver()
    try:
        # No second hostname lookup: the connector uses the validated IP records.
        # The original hostname still supplies TLS SNI and certificate validation.
        connector = aiohttp.TCPConnector(
            resolver=resolver, family=socket.AF_INET, use_dns_cache=False,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS, ceil_threshold=float("inf")),
            trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
        ) as session:
            async with session.post(
                url, json={"text": text, "mrkdwn": False,
                           "unfurl_links": False, "unfurl_media": False},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    retryable = response.status in (408, 429) or 500 <= response.status < 600
                    retry_after = None
                    header = response.headers.get("Retry-After", "").strip()
                    # Slack specifies whole seconds. Ignore malformed values;
                    # reject unrepresentable delays rather than retrying early.
                    if retryable and re.fullmatch(r"[0-9]+", header):
                        digits = header.lstrip("0") or "0"
                        if len(digits) > 10 or int(digits) > 2147483647:
                            raise SlackSendError("retry_after_out_of_range")
                        retry_after = int(digits)
                    raise SlackSendError(f"http_{response.status}",
                                         retryable=retryable, retry_after=retry_after)
                # Slack returns 'ok'. Read at most four bytes, including EOF for
                # the expected short body, under the same total request timeout.
                try:
                    body = await response.content.readexactly(4)
                except asyncio.IncompleteReadError as error:
                    body = error.partial
                if body not in (b"ok", b"ok\n"):
                    raise SlackSendError("unexpected_response")
    except TimeoutError:
        raise SlackSendError("http_timeout", retryable=True) from None
    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError):
        raise SlackSendError("tls_validation_failed") from None
    except (aiohttp.ClientError, OSError, ValueError):
        raise SlackSendError("http_transport_error", retryable=True) from None
    finally:
        await resolver.close()
