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

from gateway.providers import Effect, Refused, TimeBudget, UnknownOutcome, search, write

ADDRESS = re.compile(r"[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,4}")
API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
THREAD_ID = re.compile(r"[0-9a-fA-F]+")
MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
REPLY_PREFIX = re.compile(r"^(?:re:\s*)+", re.IGNORECASE)


def thread_subject(subject: str) -> str:
    """The subject Gmail threads on: leading "Re:" prefixes dropped, whitespace collapsed."""
    return " ".join(REPLY_PREFIX.sub("", subject.strip(), count=1).split())


def message_id(mailbox: str, idempotency_key: str) -> str:
    """A stable Message-ID per action, so an earlier send can be found again."""
    digest = sha256(idempotency_key.encode()).hexdigest()[:40]
    return f"<switchboard.{digest}@{mailbox.rsplit('@', 1)[1]}>"


async def send_reply(
    client: httpx.AsyncClient,
    mailbox: str,
    token: str,
    dispatch: dict[str, Any],
    budget: TimeBudget,
) -> Effect:
    """Send the approved draft once. ``dispatch`` is core's consumed-grant payload."""
    draft = dispatch.get("draft")
    key = dispatch.get("idempotency_key")
    if not isinstance(draft, dict) or not isinstance(key, str) or not key:
        raise Refused("Approved payload has no draft")
    recipient, body, subject = draft.get("recipient"), draft.get("body"), draft.get("subject")
    thread_id, parent, references, original = (
        draft.get("thread_id"),
        draft.get("in_reply_to"),
        draft.get("references"),
        draft.get("thread_subject"),
    )
    if (
        draft.get("channel") != "email"
        or not isinstance(recipient, str)
        or not ADDRESS.fullmatch(recipient)
        or not isinstance(body, str)
        or not body.strip()
        or not ADDRESS.fullmatch(mailbox)
        or not isinstance(subject, str)
        or not subject.strip()
        or not isinstance(thread_id, str)
        or not THREAD_ID.fullmatch(thread_id)
        or not isinstance(parent, str)
        or not MESSAGE_ID.fullmatch(parent)
        or not isinstance(references, str)
        or not references.split()
        or references.split()[-1] != parent
        or "\r" in references
        or "\n" in references
        or not all(MESSAGE_ID.fullmatch(item) for item in references.split())
    ):
        raise Refused("Approved draft is not a sendable email")
    # Gmail only keeps a reply in its thread when the subjects match.
    if (
        not isinstance(original, str)
        or not thread_subject(original)
        or "\r" in original
        or "\n" in original
        or thread_subject(subject) != thread_subject(original)
    ):
        raise Refused("Reply subject does not match the original thread's subject")
    identifier = message_id(mailbox, key)
    message = EmailMessage()
    try:
        # EmailMessage refuses header values that contain line breaks.
        message["From"] = mailbox
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = identifier
        message["In-Reply-To"] = parent
        message["References"] = references
    except ValueError:
        raise Refused("Approved draft has an invalid header value") from None
    message.set_content(body)
    headers = {"Authorization": f"Bearer {token}"}

    found = await search(
        client,
        "GET",
        API,
        budget=budget,
        headers=headers,
        params={"q": f"rfc822msgid:{identifier.strip('<>')}", "labelIds": "SENT", "maxResults": 1},
    )
    existing = found.get("messages", [])
    if not isinstance(existing, list) or (
        not existing and (found.get("resultSizeEstimate") != 0 or found.get("nextPageToken"))
    ):
        raise Refused("Message search returned an unexpected shape")
    if existing:
        try:
            external_ref = existing[0]["id"]
            if not isinstance(external_ref, str) or not external_ref:
                raise ValueError("Invalid message ID")
            found_thread = existing[0].get("threadId")
        except (KeyError, TypeError, IndexError, ValueError, AttributeError):
            raise Refused("Message search returned an unexpected shape") from None
        if found_thread != thread_id:
            # The message exists, but not where the approved reply belongs.
            raise UnknownOutcome("Earlier send is not in the approved thread")
        return Effect(external_ref=external_ref, reused=True)

    sent = await write(
        client,
        "POST",
        f"{API}/send",
        budget=budget,
        headers=headers,
        json={"raw": urlsafe_b64encode(message.as_bytes()).decode(), "threadId": thread_id},
    )
    if not isinstance(sent.get("id"), str) or not sent["id"]:
        raise UnknownOutcome("Send response had no message ID")
    if sent.get("threadId") != thread_id:
        raise UnknownOutcome("Message was not sent in the approved thread")
    return Effect(external_ref=sent["id"], reused=False)
