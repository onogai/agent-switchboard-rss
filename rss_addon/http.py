"""Bounded HTTP transport for public feeds and, later, article pages."""

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from yarl import URL

import aiohttp
from aiohttp.resolver import ThreadedResolver


class FetchError(RuntimeError):
    pass


class UnsafeDestination(FetchError):
    pass


class HTTPError(FetchError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


def public_ip(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def validate_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise UnsafeDestination("Invalid URL")
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        raise UnsafeDestination("Invalid URL characters")
    try:
        # Validate the same canonical representation aiohttp connects to.
        parsed = URL(url)
        host = parsed.raw_host
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestination("Invalid URL") from exc
    if (parsed.scheme not in ("http", "https") or not host or
            parsed.user is not None or parsed.password is not None or
            port not in (None, 80, 443)):
        raise UnsafeDestination("Expected public HTTP(S) URL on port 80 or 443")
    host = host.lower().rstrip(".")
    if "%" in host:
        raise UnsafeDestination("Invalid host")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafeDestination("Local destination")
    try:
        if not public_ip(host):
            raise UnsafeDestination("Non-public IP address")
    except ValueError:
        pass
    return str(parsed)


class PublicResolver(ThreadedResolver):
    """Check the addresses actually used by aiohttp, not a separate DNS lookup."""

    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(not public_ip(item["host"]) for item in addresses):
            raise UnsafeDestination("DNS resolved to a non-public address")
        return addresses


@dataclass(frozen=True)
class Page:
    url: str
    body: bytes


class HTTPClient:
    def __init__(self, *, concurrency=5, timeout=25, max_bytes=2 * 1024 * 1024,
                 retries=1, max_redirects=5):
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.max_redirects = max_redirects
        self._slots = asyncio.Semaphore(concurrency)
        self._session = None
        self._resolver = None

    async def __aenter__(self):
        self._resolver = PublicResolver()
        connector = aiohttp.TCPConnector(
            resolver=self._resolver, limit=self.concurrency, ttl_dns_cache=60,
        )
        self._session = aiohttp.ClientSession(
            connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": "SwitchboardRSS/0.1"},
        )
        return self

    async def __aexit__(self, *args):
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._resolver is not None:
            await self._resolver.close()
            self._resolver = None

    async def fetch(self, url, *, max_bytes=None, method="GET", data=None, headers=None,
                    allowed_host=None):
        url = validate_url(url)
        if self._session is None:
            raise RuntimeError("HTTPClient must be opened first")
        budget = self.max_bytes if max_bytes is None else max_bytes
        if budget <= 0:
            raise ValueError("Invalid download limit")
        async with self._slots:
            try:
                async with asyncio.timeout(self.timeout):
                    for attempt in range(self.retries + 1):
                        try:
                            return await self._download(url, budget, method, data, headers, allowed_host)
                        except HTTPError as exc:
                            if exc.status != 429 and not 500 <= exc.status <= 599:
                                raise
                            if attempt == self.retries:
                                raise
                        except aiohttp.ClientError as exc:
                            if attempt == self.retries:
                                raise FetchError(type(exc).__name__) from exc
                        await asyncio.sleep(0.5 * 2 ** attempt)
            except TimeoutError as exc:
                raise FetchError("Request timed out") from exc

    async def _download(self, url, budget, method, data, headers, allowed_host):
        for hop in range(self.max_redirects + 1):
            url = validate_url(url)
            if allowed_host is not None and (urlsplit(url).hostname != allowed_host
                                             or urlsplit(url).scheme != "https"):
                raise UnsafeDestination("Unexpected redirect destination")
            async with self._session.request(method, url, data=data, headers=headers,
                                             allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    if method != "GET":
                        raise FetchError("Unexpected POST redirect")
                    location = response.headers.get("Location")
                    if not location or hop == self.max_redirects:
                        raise FetchError("Missing redirect location or too many redirects")
                    url = urljoin(str(response.url), location)
                    continue
                if not 200 <= response.status < 300:
                    raise HTTPError(response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    if len(body) + len(chunk) > budget:
                        raise FetchError("Download exceeds byte limit")
                    body.extend(chunk)
                return Page(str(response.url), bytes(body))
        raise FetchError("Too many redirects")
