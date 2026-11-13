from hmac import compare_digest, digest
from json import loads
from time import time
from urllib.parse import parse_qs
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from app.adapters.db import Settings

BUTTONS = {
    "switchboard_approve": "approved",
    "switchboard_reject": "rejected",
    "switchboard_escalate": "escalated",
}


class SlackAuthenticationError(Exception):
    pass


class SlackPayloadError(Exception):
    pass


class SlackIdentity(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class SlackContainer(BaseModel):
    channel_id: str = Field(min_length=1, max_length=64)
    message_ts: str = Field(pattern=r"^[0-9]{1,16}\.[0-9]{6}$")
    type: str


class SlackButton(BaseModel):
    action_id: str
    type: str
    value: UUID


class SlackInteraction(BaseModel):
    type: str
    api_app_id: str
    team: SlackIdentity
    user: SlackIdentity
    container: SlackContainer
    actions: list[SlackButton] = Field(min_length=1, max_length=1)


def verify_interaction(
    config: Settings, body: bytes, timestamp: str, signature: str
) -> SlackInteraction:
    if (
        not config.slack_app_id
        or not config.slack_signing_secret
        or not timestamp.isascii()
        or not timestamp.isdecimal()
        or len(timestamp) > 12
        or abs(time() - int(timestamp)) > 300
        or not signature.isascii()
    ):
        raise SlackAuthenticationError
    signed = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + digest(config.slack_signing_secret.encode(), signed, "sha256").hex()
    if not compare_digest(expected, signature):
        raise SlackAuthenticationError
    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=1)
        if len(form.get("payload", [])) != 1:
            raise SlackPayloadError
        event = SlackInteraction.model_validate(loads(form["payload"][0]))
    except (ValueError, ValidationError, RecursionError):
        raise SlackPayloadError from None
    if event.api_app_id != config.slack_app_id:
        raise SlackAuthenticationError
    button = event.actions[0]
    if (
        event.type != "block_actions"
        or event.container.type != "message"
        or button.type != "button"
        or button.action_id not in BUTTONS
    ):
        raise SlackPayloadError
    return event
