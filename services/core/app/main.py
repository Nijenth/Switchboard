from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from re import fullmatch
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import DataError, Error, IntegrityError, OperationalError
from psycopg.errors import InsufficientPrivilege, RaiseException
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.adapters.db import Settings, create_pool
from app.adapters.ingest import (
    IngestIdempotencyConflict,
    IngressScopeMismatch,
    InvalidIngressCredential,
    ingest,
)
from app.adapters.slack import (
    BUTTONS,
    SlackAuthenticationError,
    SlackPayloadError,
    verify_interaction,
)
from app.api.approvals import approval_router
from app.api.audit import audit_router
from app.api.decide import decide_router
from app.api.drafts import draft_router
from app.api.execution import execution_router
from app.api.lookups import lookup_router
from app.api.refunds import refund_router
from app.api.retrieve import retrieve_router
from app.api.understand import understand_router
from app.schemas.ingest import IngestInput, IngestResult


class Health(BaseModel):
    status: str
    database: str
    trace_id: UUID


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not token.isascii():
        raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
    return token


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings.from_environment()
    pool = create_pool(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(execution_router(pool, config.execution_credentials, bearer_token))
    app.include_router(audit_router(pool, config.audit_token, bearer_token))
    app.include_router(decide_router(pool, config.decide_credentials, bearer_token))
    app.include_router(
        draft_router(pool, config.draft_credentials, config.offline_gateway_url, bearer_token)
    )
    app.include_router(retrieve_router(pool, config.retrieve_credentials, bearer_token))
    app.include_router(lookup_router(pool, config.lookup_credentials, bearer_token))
    app.include_router(refund_router(pool, config.refund_credentials, bearer_token))
    app.include_router(approval_router(pool, config.approval_credentials, bearer_token))
    app.include_router(
        understand_router(
            pool,
            config.understand_credentials,
            config.offline_gateway_url,
            config.understand_primary,
            bearer_token,
        )
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.get("/health", response_model=Health)
    def health(
        authorization: Annotated[str | None, Header()] = None,
        x_trace_id: Annotated[UUID | None, Header()] = None,
    ) -> Health:
        if not compare_digest(bearer_token(authorization), config.health_token):
            raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
        trace_id = x_trace_id or uuid4()
        try:
            with pool.connection() as connection:
                connection.execute("SELECT 1")
        except (Error, TooManyRequests):
            raise HTTPException(
                503, {"status": "unavailable", "database": "unavailable", "trace_id": str(trace_id)}
            ) from None
        return Health(status="ok", database="ok", trace_id=trace_id)

    @app.post("/ingest", response_model=IngestResult)
    def ingest_message(
        payload: IngestInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        token = bearer_token(authorization)
        if not fullmatch(r"[0-9a-f]{64}", token):
            raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
        try:
            with pool.connection() as connection, connection.transaction():
                return ingest(connection, payload, token)
        except InvalidIngressCredential:
            raise HTTPException(
                401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"}
            ) from None
        except IngressScopeMismatch:
            raise HTTPException(403, "Ingress scope mismatch") from None
        except IngestIdempotencyConflict:
            raise HTTPException(
                409, "Idempotency key already used for a different request"
            ) from None
        except DataError:
            raise HTTPException(422, "Invalid ingest data") from None
        except IntegrityError:
            raise HTTPException(409, "Ingest data conflicts with existing records") from None
        except (OperationalError, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Ingest unavailable") from None
        except Error:
            raise HTTPException(500, "Ingest failed") from None

    @app.post("/approvals/slack")
    async def slack_approval(request: Request) -> dict[str, UUID]:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65_536:
                raise HTTPException(413, "Slack request too large")
        try:
            event = verify_interaction(
                config,
                bytes(body),
                request.headers.get("X-Slack-Request-Timestamp", ""),
                request.headers.get("X-Slack-Signature", ""),
            )
        except SlackAuthenticationError:
            raise HTTPException(401, "Invalid Slack signature or app") from None
        except SlackPayloadError:
            raise HTTPException(422, "Invalid Slack interaction") from None

        def record() -> dict[str, UUID]:
            button = event.actions[0]
            try:
                with pool.connection() as connection, connection.transaction():
                    row = connection.execute(
                        "SELECT decision_id, tenant_id FROM public.record_slack_approval(%s,%s,%s,%s,%s,%s)",
                        (
                            button.value,
                            event.team.id,
                            event.user.id,
                            event.container.channel_id,
                            event.container.message_ts,
                            BUTTONS[button.action_id],
                        ),
                    ).fetchone()
                    assert row is not None
                    return {"decision_id": row[0], "tenant_id": row[1]}
            except InsufficientPrivilege as error:
                if (
                    BUTTONS[button.action_id] == "approved"
                    and error.diag.message_primary == "Slack reviewer lacks the required role"
                ):
                    raise HTTPException(409, "Reviewer cannot satisfy the approval role") from None
                raise HTTPException(403, "Reviewer is not authorized") from None
            except (DataError, IntegrityError, RaiseException):
                raise HTTPException(409, "Approval could not be recorded") from None
            except (OperationalError, PoolTimeout, TooManyRequests):
                raise HTTPException(503, "Approval unavailable") from None

        return await run_in_threadpool(record)

    return app
