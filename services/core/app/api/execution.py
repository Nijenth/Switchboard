from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from secrets import token_hex
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg import Connection, DataError, Error, IntegrityError, OperationalError
from psycopg.errors import InsufficientPrivilege, RaiseException
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

GrantToken = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\x00]+$")]


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    role: Literal["worker", "reconciler"]
    principal: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    token: GrantToken = Field(repr=False)


class Consume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: GrantToken


class Complete(Consume):
    external_ref: Reference


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["found", "absent", "inconclusive"]
    lookup_ref: Reference
    external_ref: Reference | None = None

    @model_validator(mode="after")
    def check_reference(self) -> "Evidence":
        if (self.outcome == "found") != (self.external_ref is not None):
            raise ValueError("Only a found result carries an external reference")
        return self


class ClaimResult(BaseModel):
    action_id: UUID
    grant: GrantToken
    expires_at: datetime
    trace_id: UUID


def execution_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter(prefix="/actions")
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid execution credential configuration") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Execution credentials must be unique")
    if len({(entry.tenant_id, entry.role, entry.principal) for entry in credentials}) != len(
        credentials
    ):
        raise ValueError("Execution principals must be unique in their tenant and role")

    def authenticate(role: Literal["worker", "reconciler"]) -> Callable[..., Credential]:
        def require(authorization: Annotated[str | None, Header()] = None) -> Credential:
            token = bearer_token(authorization)
            for entry in credentials:
                if entry.role == role and compare_digest(token, entry.token):
                    return entry
            raise HTTPException(403, "Execution credential is not authorized")

        return require

    worker = authenticate("worker")
    reconciler = authenticate("reconciler")

    def transition(credential: Credential, query: str, params: tuple[object, ...]) -> object:
        try:
            with pool.connection() as connection, connection.transaction():
                connection.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (str(credential.tenant_id),)
                )
                result = connection.execute(query, params).fetchone()
                return result[0] if result is not None else None
        except InsufficientPrivilege:
            raise HTTPException(403, "Execution is not authorized") from None
        except DataError:
            raise HTTPException(422, "Invalid execution request") from None
        except (IntegrityError, RaiseException):
            raise HTTPException(409, "Execution transition refused") from None
        except (OperationalError, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Execution unavailable") from None
        except Error:
            raise HTTPException(500, "Execution failed") from None

    @router.post("/{action_id}/claim")
    def claim(
        action_id: UUID,
        credential: Annotated[Credential, Depends(worker)],
        x_trace_id: Annotated[UUID | None, Header()] = None,
    ) -> ClaimResult:
        grant = token_hex(32)
        expires = transition(
            credential,
            "SELECT public.claim_mock_action(%s,%s,%s)",
            (action_id, credential.principal, sha256(grant.encode("ascii")).digest()),
        )
        if not isinstance(expires, datetime):
            raise HTTPException(409, "Action could not be claimed")
        return ClaimResult(
            action_id=action_id, grant=grant, expires_at=expires, trace_id=x_trace_id or uuid4()
        )

    @router.post("/{action_id}/consume")
    def consume(
        action_id: UUID,
        body: Consume,
        credential: Annotated[Credential, Depends(worker)],
    ) -> object:
        dispatch = transition(
            credential,
            "SELECT public.consume_mock_grant(%s,%s,%s)",
            (action_id, credential.principal, sha256(body.grant.encode("ascii")).digest()),
        )
        if dispatch is None:
            raise HTTPException(409, "Grant cannot be consumed")
        return dispatch

    @router.post("/{action_id}/complete")
    def complete(
        action_id: UUID,
        body: Complete,
        credential: Annotated[Credential, Depends(worker)],
    ) -> dict[str, str]:
        result = transition(
            credential,
            "SELECT public.complete_mock_action(%s,%s,%s,%s)",
            (
                action_id,
                credential.principal,
                sha256(body.grant.encode("ascii")).digest(),
                body.external_ref,
            ),
        )
        if result is not True:
            raise HTTPException(409, "Grant cannot complete this action")
        return {"status": "executed"}

    @router.post("/{action_id}/reconcile")
    def reconcile(
        action_id: UUID,
        credential: Annotated[Credential, Depends(reconciler)],
        body: Evidence | None = None,
    ) -> dict[str, str]:
        evidence = (
            (body.outcome, body.lookup_ref, body.external_ref) if body else (None, None, None)
        )
        status = transition(
            credential,
            "SELECT public.reconcile_mock_action(%s,%s,%s,%s)",
            (action_id, *evidence),
        )
        if not isinstance(status, str):
            raise HTTPException(409, "Action cannot be reconciled")
        return {"status": status}

    return router
