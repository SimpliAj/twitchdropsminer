"""String manipulation utility functions."""

from __future__ import annotations

import random
import string
from collections import OrderedDict, abc
from typing import TypeVar


# Character sets for nonce generation
CHARS_ASCII = string.ascii_letters + string.digits
CHARS_HEX_LOWER = string.digits + "abcdef"
CHARS_HEX_UPPER = string.digits + "ABCDEF"

_T = TypeVar("_T")


def create_nonce(chars: str, length: int) -> str:
    """Generate a random nonce string of specified length from given characters."""
    return "".join(random.choices(chars, k=length))


def chunk(to_chunk: abc.Iterable[_T], chunk_length: int) -> abc.Generator[list[_T], None, None]:
    """Split an iterable into chunks of a specified length."""
    list_to_chunk: list[_T] = list(to_chunk)
    for i in range(0, len(list_to_chunk), chunk_length):
        yield list_to_chunk[i : i + chunk_length]


def deduplicate(iterable: abc.Iterable[_T]) -> list[_T]:
    """Remove duplicates from an iterable while preserving order."""
    return list(OrderedDict.fromkeys(iterable).keys())


PROXY_MASK = "****"


def mask_proxy_url(url: str) -> str:
    """Replace the password in a proxy URL's credentials with a fixed mask."""
    if not url or "@" not in url:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    userinfo, at, host = rest.rpartition("@")
    if ":" not in userinfo:
        return url
    user = userinfo.split(":", 1)[0]
    return f"{scheme + sep}{user}:{PROXY_MASK}{at}{host}"


def is_masked_proxy_url(url: str) -> bool:
    """True if the URL carries the mask in place of a real password."""
    return bool(url) and mask_proxy_url(url) == url and f":{PROXY_MASK}@" in url


def mask_text(text: str, proxy_url: str) -> str:
    """Hide a proxy URL's real credentials wherever it appears in free text."""
    if proxy_url and proxy_url != mask_proxy_url(proxy_url):
        text = text.replace(proxy_url, mask_proxy_url(proxy_url))
    return text
