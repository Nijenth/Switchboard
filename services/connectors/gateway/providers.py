"""Shared rules for provider calls: timeouts, search retries, and outcome classes."""

from dataclasses import dataclass
from time import sleep
from typing import Any

import httpx

SEARCH_ATTEMPTS = 3


class Refused(Exception):
    """The gateway is certain nothing was written. Safe to report as not executed."""


class UnknownOutcome(Exception):
    """A write may or may not have happened. Never retry; leave it to the reconciler."""


class _Unreadable(Exception):
    pass


@dataclass(frozen=True)
class Effect:
    """The provider-side record of one executed action."""

    external_ref: str
    reused: bool  # True when an earlier attempt with the same idempotency key was found


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _Unreadable
    return body


def search(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    timeout: float,
    backoff: float = 0.5,
    **request: Any,
) -> dict[str, Any]:
    """A call that changes nothing. Retries on 429 and 5xx with exponential backoff.

    Raises Refused on any failure: no write has been attempted yet at this point.
    """
    for attempt in range(SEARCH_ATTEMPTS):
        try:
            response = client.request(method, url, timeout=timeout, **request)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code < 400:
            try:
                return _json(response)
            except _Unreadable:
                raise Refused("Provider search returned an unreadable body") from None
        if response is not None and response.status_code != 429 and response.status_code < 500:
            raise Refused(f"Provider search was rejected with HTTP {response.status_code}")
        if attempt + 1 < SEARCH_ATTEMPTS:
            sleep(backoff * 2**attempt)
    raise Refused("Provider search failed after retries")


def write(
    client: httpx.Client, method: str, url: str, *, timeout: float, **request: Any
) -> dict[str, Any]:
    """A call that changes provider state. Sent exactly once.

    A 4xx other than 408 and 429 means the provider rejected it: Refused.
    Anything else that is not a clean success is UnknownOutcome.
    """
    try:
        response = client.request(method, url, timeout=timeout, **request)
    except httpx.HTTPError:
        raise UnknownOutcome("No answer from the provider after sending the write") from None
    status = response.status_code
    if 400 <= status < 500 and status not in (408, 429):
        raise Refused(f"Provider rejected the write with HTTP {status}")
    if status >= 300:
        raise UnknownOutcome(f"Provider answered HTTP {status} to the write")
    try:
        return _json(response)
    except _Unreadable:
        raise UnknownOutcome("Provider accepted the write but the body was unreadable") from None
