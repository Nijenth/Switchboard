"""send_reply through the Gmail API.

Request and response shapes follow the public API documentation and have not
been exercised against a real mailbox (ADR 0032, "Known limits").
"""

import re
from base64 import urlsafe_b64encode
from email.message import EmailMessage
from hashlib import sha256
from typing import Any

import httpx

from gateway.providers import Effect, Refused, UnknownOutcome, search, write

ADDRESS = re.compile(r"[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,4}")
API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
DEFAULT_SUBJECT = "Re: your message"


def message_id(mailbox: str, idempotency_key: str) -> str:
    """A stable Message-ID per action, so an earlier send can be found again."""
    digest = sha256(idempotency_key.encode()).hexdigest()[:40]
    return f"<switchboard.{digest}@{mailbox.rsplit('@', 1)[1]}>"


def send_reply(
    client: httpx.Client, mailbox: str, token: str, dispatch: dict[str, Any], timeout: float
) -> Effect:
    """Send the approved draft once. ``dispatch`` is core's consumed-grant payload."""
    draft = dispatch.get("draft")
    key = dispatch.get("idempotency_key")
    if not isinstance(draft, dict) or not isinstance(key, str) or not key:
        raise Refused("Approved payload has no draft")
    recipient, body, subject = draft.get("recipient"), draft.get("body"), draft.get("subject")
    if (
        draft.get("channel") != "email"
        or not isinstance(recipient, str)
        or not ADDRESS.fullmatch(recipient)
        or not isinstance(body, str)
        or not body.strip()
        or not ADDRESS.fullmatch(mailbox)
    ):
        raise Refused("Approved draft is not a sendable email")
    identifier = message_id(mailbox, key)
    message = EmailMessage()
    try:
        # EmailMessage refuses header values that contain line breaks.
        message["From"] = mailbox
        message["To"] = recipient
        message["Subject"] = subject if isinstance(subject, str) and subject else DEFAULT_SUBJECT
        message["Message-ID"] = identifier
    except ValueError:
        raise Refused("Approved draft has an invalid header value") from None
    message.set_content(body)
    headers = {"Authorization": f"Bearer {token}"}

    found = search(
        client,
        "GET",
        API,
        timeout=timeout,
        headers=headers,
        params={"q": f"rfc822msgid:{identifier.strip('<>')}", "maxResults": 1},
    )
    existing = found.get("messages") or []
    if existing:
        try:
            return Effect(external_ref=str(existing[0]["id"]), reused=True)
        except (KeyError, TypeError, IndexError):
            raise Refused("Message search returned an unexpected shape") from None

    sent = write(
        client,
        "POST",
        f"{API}/send",
        timeout=timeout,
        headers=headers,
        json={"raw": urlsafe_b64encode(message.as_bytes()).decode()},
    )
    if not isinstance(sent.get("id"), str) or not sent["id"]:
        raise UnknownOutcome("Send response had no message ID")
    return Effect(external_ref=sent["id"], reused=False)
