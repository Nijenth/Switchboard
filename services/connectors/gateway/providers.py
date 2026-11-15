"""Shared rules for provider calls: timeouts, search retries, and outcome classes."""

from asyncio import sleep
from asyncio import timeout as request_timeout
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

SEARCH_ATTEMPTS = 3
TimeBudget = Callable[[], float]


class Refused(Exception):
    """The gateway is certain nothing was written. Safe to report as not executed."""


class UnknownOutcome(Exception):
    """A write may or may not have happened. Never retry; leave it to the reconciler."""


class ProviderFailed(Exception):
    """The provider recorded the attempt, and its transactions failed."""


class LeaseElapsed(Refused):
    """Too little time remains to start another request."""


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
    except Exception:  # noqa: BLE001 - treat any decoder failure (e.g. RecursionError) as unreadable
        body = None
    if not isinstance(body, dict):
        raise _Unreadable
    return body


async def search(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    budget: TimeBudget,
    backoff: float = 0.5,
    **request: Any,
) -> dict[str, Any]:
    """A call that changes nothing. Retries on 429 and 5xx with exponential backoff.

    Raises Refused if the search fails before receiving a 2xx body, or UnknownOutcome
    if a 2xx response body is unreadable.
    """
    for attempt in range(SEARCH_ATTEMPTS):
        seconds = budget()
        try:
            async with request_timeout(seconds):
                response = await client.request(
                    method, url, timeout=seconds, follow_redirects=False, **request
                )
        except (httpx.HTTPError, TimeoutError):
            response = None
        if response is not None and response.is_success:
            try:
                return _json(response)
            except _Unreadable:
                raise UnknownOutcome("Provider search returned an unreadable body") from None
        if response is not None and response.status_code != 429 and response.status_code < 500:
            raise Refused(f"Provider search was rejected with HTTP {response.status_code}")
        if attempt + 1 < SEARCH_ATTEMPTS:
            await sleep(backoff * 2**attempt)
    raise Refused("Provider search failed after retries")


async def write(
    client: httpx.AsyncClient, method: str, url: str, *, budget: TimeBudget, **request: Any
) -> dict[str, Any]:
    """A call that changes provider state. Sent exactly once.

    Any non-2xx status or unreadable body is an UnknownOutcome; the request is never retried.
    """
    seconds = budget()
    try:
        async with request_timeout(seconds):
            response = await client.request(
                method, url, timeout=seconds, follow_redirects=False, **request
            )
    except (httpx.HTTPError, TimeoutError):
        raise UnknownOutcome("No answer from the provider after sending the write") from None
    if not response.is_success:
        raise UnknownOutcome(f"Provider answered HTTP {response.status_code} to the write")
    try:
        return _json(response)
    except _Unreadable:
        raise UnknownOutcome("Provider accepted the write but the body was unreadable") from None
