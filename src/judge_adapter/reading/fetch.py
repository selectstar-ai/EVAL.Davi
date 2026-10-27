# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Fetch one web page for ``davi-read``, with limits.

- http and https only; redirects are followed by hand (at most
  ``max_redirects``) and every hop is checked again.
- Hosts that resolve to private, loopback, link-local or other reserved
  addresses are refused unless listed in ``allow_hosts`` (an intranet wiki,
  for example). This protects the reader itself: an agent should not be
  able to read an internal address and send it to the judge by accident.
  The check resolves the name before the request; a DNS answer that
  changes in between is not caught.
- The body is read up to ``max_bytes``; a larger page is refused.
- HTML and text types are decoded (charset from the header, then a
  ``<meta charset>``, then UTF-8); PDFs, images and other files are
  refused with their type.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = (
    'Mozilla/5.0 (compatible; davi-read; '
    '+https://github.com/selectstar-ai/EVAL.Davi)'
)
TEXT_TYPES = frozenset(
    {
        'text/html',
        'application/xhtml+xml',
        'text/plain',
        'text/markdown',
        'text/x-markdown',
    }
)
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_META_CHARSET_RE = re.compile(
    rb'<meta[^>]+charset=["\']?([A-Za-z0-9_.:-]+)', re.IGNORECASE
)


class FetchError(Exception):
    """A page that could not be read. ``code`` is one of blocked, network,
    http, too_large, unsupported."""

    def __init__(self, code, message):
        super().__init__(f'{code}: {message}')
        self._code = code
        self._message = message

    @property
    def code(self):
        return self._code

    @property
    def message(self):
        return self._message


@dataclass(frozen=True)
class Page:
    url: str
    final_url: str
    media_type: str
    text: str


def _media_type(response):
    value = response.headers.get('content-type', '')
    return value.split(';')[0].strip().lower()


def _charset(response, head):
    value = response.headers.get('content-type', '')
    match = re.search(r'charset=["\']?([A-Za-z0-9_.:-]+)', value, re.I)
    if match:
        return match.group(1)
    match = _META_CHARSET_RE.search(head)
    return match.group(1).decode('ascii') if match else 'utf-8'


def _decode(data, charset):
    try:
        return data.decode(charset, errors='replace')
    except LookupError:
        return data.decode('utf-8', errors='replace')


class PageFetcher:
    """``fetch(url) -> Page`` or ``FetchError``."""

    def __init__(
        self,
        allow_hosts=(),
        *,
        max_bytes=5_000_000,
        timeout=20.0,
        max_redirects=5,
        transport=None,
        resolver=socket.getaddrinfo,
    ):
        self._allow = {host.lower() for host in allow_hosts if host}
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._transport = transport
        self._resolver = resolver

    def fetch(self, url):
        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
            headers={'User-Agent': USER_AGENT},
        ) as client:
            current = url
            for _ in range(self._max_redirects + 1):
                self.check(current)
                try:
                    with client.stream('GET', current) as response:
                        if response.status_code in REDIRECTS:
                            location = response.headers.get('location')
                            if not location:
                                raise FetchError(
                                    'http', 'redirect without a location'
                                )
                            current = urljoin(current, location)
                            continue
                        return self._read(url, current, response)
                except httpx.TimeoutException:
                    raise FetchError(
                        'network', f'no reply within {self._timeout:g} s'
                    ) from None
                except httpx.HTTPError as error:
                    raise FetchError(
                        'network', f'request failed: {type(error).__name__}'
                    ) from None
        raise FetchError('http', 'too many redirects')

    def check(self, url):
        """Refuse non-http(s) URLs and hosts on private addresses."""
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise FetchError('blocked', 'only http and https URLs are read')
        host = parts.hostname.lower()
        if host in self._allow:
            return
        try:
            addresses = {
                info[4][0] for info in self._resolver(host, parts.port or 443)
            }
        except (OSError, UnicodeError):
            raise FetchError('network', f'cannot resolve {host}') from None
        for address in addresses:
            ip = ipaddress.ip_address(address.split('%')[0])
            if not ip.is_global:
                raise FetchError(
                    'blocked',
                    f'{host} is a private or local address; add it to '
                    'the allowed hosts to read it',
                )

    def _read(self, url, final_url, response):
        if response.status_code != 200:
            raise FetchError('http', f'HTTP {response.status_code}')
        media = _media_type(response)
        if media and media not in TEXT_TYPES and not media.startswith('text/'):
            raise FetchError(
                'unsupported', f'{media} is not an HTML or text page'
            )
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > self._max_bytes:
                raise FetchError(
                    'too_large', f'the page is over {self._max_bytes} bytes'
                )
        body = bytes(data)
        if body.startswith(b'%PDF-'):
            raise FetchError('unsupported', 'application/pdf is not a page')
        text = _decode(body, _charset(response, body[:4096]))
        return Page(url, final_url, media or 'text/html', text)
