"""POST /execute: claim, consume, check, call the provider once, complete (ADR 0032)."""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from hmac import compare_digest
from os import environ
from time import monotonic
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from gateway import gmail, shopify
from gateway.providers import Effect, LeaseElapsed, ProviderFailed, Refused, UnknownOutcome

log = logging.getLogger("switchboard.gateway")
Token = Annotated[str, Field(min_length=32, max_length=512, pattern=r"^[\x21-\x7e]+$", repr=False)]
# Leave time to record completion inside the lease.
COMPLETION_RESERVE_SECONDS = 8.0
MAX_PROVIDER_TIMEOUT_SECONDS = 15.0
CORE_TIMEOUT_SECONDS = 5.0


class Caller(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    token: Token


class Account(BaseModel):
    """One provider credential, valid for one tenant, tool and destination account."""

    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    tool_key: Literal["order_refund_issue", "send_reply"]
    provider: Literal["shopify", "gmail"]
    destination: str = Field(min_length=1, max_length=254)
    token: Token
    # Shopify only: https://<shop>.myshopify.com/admin/api/<version>/graphql.json
    endpoint: HttpUrl | None = None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    core_url: HttpUrl
    core_worker_tokens: list[Caller]  # the gateway's own worker credential per tenant
    callers: list[Caller]  # who may ask the gateway to execute, per tenant
    accounts: list[Account]

    @classmethod
    def from_environment(cls) -> "Settings":
        try:
            return cls.model_validate_json(environ["SWITCHBOARD_GATEWAY_CONFIG"])
        except (KeyError, ValidationError):
            # Never echo the configuration: it holds credentials.
            raise ValueError("Invalid or missing SWITCHBOARD_GATEWAY_CONFIG") from None

    def model_post_init(self, _: Any) -> None:
        pairs = {
            ("shopify", "order_refund_issue"),
            ("gmail", "send_reply"),
        }
        tokens = [entry.token for entry in (*self.callers, *self.core_worker_tokens)]
        tokens += [account.token for account in self.accounts]
        if len(set(tokens)) != len(tokens):
            raise ValueError("Every gateway credential must be distinct")
        for account in self.accounts:
            if (account.provider, account.tool_key) not in pairs:
                raise ValueError("Provider does not implement that tool")
            if (account.provider == "shopify") != (account.endpoint is not None):
                raise ValueError("Only Shopify accounts take an endpoint")
            if account.endpoint is not None and account.endpoint.scheme != "https":
                raise ValueError("Provider endpoints must use https")
            if account.endpoint is not None and account.endpoint.host != account.destination:
                raise ValueError("Shopify endpoint host must match the destination shop")
        keys = [(a.tenant_id, a.tool_key, a.destination) for a in self.accounts]
        if len(set(keys)) != len(keys):
            raise ValueError("One credential per tenant, tool and destination")


class Execute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: UUID


class Executed(BaseModel):
    status: Literal["executed"]
    external_ref: str
    reused: bool


def create_app(
    settings: Settings,
    *,
    core: httpx.AsyncClient | None = None,
    provider: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[], float] = monotonic,
) -> FastAPI:
    core_client = core or httpx.AsyncClient(timeout=CORE_TIMEOUT_SECONDS)
    provider_client = provider or httpx.AsyncClient(follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            if core is None:
                await resources.enter_async_context(core_client)
            if provider is None:
                await resources.enter_async_context(provider_client)
            yield

    app = FastAPI(
        title="Switchboard connector gateway", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    core_url = str(settings.core_url).rstrip("/")

    def authenticate(authorization: str | None) -> UUID:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme == "Bearer" and token:
            for caller in settings.callers:
                if compare_digest(token.encode(), caller.token.encode()):
                    return caller.tenant_id
        raise HTTPException(401, "Gateway credential is not authorized")

    async def core_post(tenant: UUID, path: str, body: dict[str, Any] | None) -> httpx.Response:
        worker = next(e.token for e in settings.core_worker_tokens if e.tenant_id == tenant)
        return await core_client.post(
            core_url + path,
            json=body,
            headers={"Authorization": f"Bearer {worker}"},
            timeout=CORE_TIMEOUT_SECONDS,
        )

    @app.post("/execute")
    async def execute(
        request: Execute, authorization: Annotated[str | None, Header()] = None
    ) -> Executed:
        tenant = authenticate(authorization)
        if not any(entry.tenant_id == tenant for entry in settings.core_worker_tokens):
            raise HTTPException(503, "Gateway has no core credential for this tenant")
        action = f"/actions/{request.action_id}"
        try:
            claim = await core_post(tenant, action + "/claim", None)
            if claim.status_code != 200:
                raise HTTPException(409, "Core did not grant this action")
            grant = claim.json()["grant"]
            consumed = await core_post(
                tenant, action + "/consume", {"grant": grant, "adapter": "real"}
            )
            if consumed.status_code != 200:
                raise HTTPException(409, "Core did not release this action for real dispatch")
            dispatch = consumed.json()
            expires = datetime.fromisoformat(dispatch["expires_at"])
            if expires.utcoffset() is None:
                raise ValueError("Lease expiration must include its time zone")
            deadline = clock() + (expires - now()).total_seconds() - COMPLETION_RESERVE_SECONDS
            adapter, tool_key, destination = (
                dispatch["adapter"],
                dispatch["tool_key"],
                dispatch["destination"],
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "Core did not answer as expected") from None

        # Fail closed before touching a provider. Core decided; the gateway re-checks its own side.
        if adapter != "real":
            raise HTTPException(409, "Only grants for the real adapter can reach a provider")
        account = next(
            (
                item
                for item in settings.accounts
                if (item.tenant_id, item.tool_key, item.destination)
                == (tenant, tool_key, destination)
            ),
            None,
        )
        if account is None:
            raise HTTPException(409, "No credential for this tenant, tool and destination")

        def provider_budget() -> float:
            remaining = deadline - clock()
            if remaining < 1:
                raise LeaseElapsed("Not enough of the lease remains to call the provider")
            return min(remaining, MAX_PROVIDER_TIMEOUT_SECONDS)

        trace = dispatch.get("trace_id")
        try:
            effect: Effect
            if account.provider == "shopify":
                effect = await shopify.issue_refund(
                    provider_client, str(account.endpoint), account.token, dispatch, provider_budget
                )
            else:
                effect = await gmail.send_reply(
                    provider_client, account.destination, account.token, dispatch, provider_budget
                )
        except LeaseElapsed as error:
            raise HTTPException(409, str(error)) from None
        except ProviderFailed as error:
            log.warning(json.dumps(_event("failed", request.action_id, tool_key, trace)))
            raise HTTPException(422, {"status": "failed", "reason": str(error)}) from None
        except Refused as error:
            log.warning(json.dumps(_event("refused", request.action_id, tool_key, trace)))
            raise HTTPException(422, f"Not executed: {error}") from None
        except UnknownOutcome:
            log.error(json.dumps(_event("unknown_outcome", request.action_id, tool_key, trace)))
            raise HTTPException(
                502, "Provider outcome unknown. Do not retry; reconciliation will decide."
            ) from None

        try:
            completed = await core_post(
                tenant,
                action + "/complete",
                {"grant": grant, "adapter": "real", "external_ref": effect.external_ref},
            )
            recorded = completed.status_code == 200
        except httpx.HTTPError:
            recorded = False
        if not recorded:
            # The effect exists and carries the idempotency key. The reconciler will find it.
            log.error(json.dumps(_event("completion_lost", request.action_id, tool_key, trace)))
            raise HTTPException(502, "Executed, but completion was not recorded")
        log.info(json.dumps(_event("executed", request.action_id, tool_key, trace)))
        return Executed(status="executed", external_ref=effect.external_ref, reused=effect.reused)

    return app


def _event(name: str, action_id: UUID, tool_key: object, trace_id: object) -> dict[str, str]:
    # IDs only. Never payloads, recipients, bodies or tokens.
    return {
        "event": f"gateway.{name}",
        "action_id": str(action_id),
        "tool_key": str(tool_key),
        "trace_id": str(trace_id),
    }
