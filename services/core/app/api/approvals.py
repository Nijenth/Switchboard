from collections.abc import Callable
from hmac import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, DataError, Error, IntegrityError, OperationalError
from psycopg.errors import InsufficientPrivilege, RaiseException
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from app.ai_accounting import set_scope
from app.api.execution import GrantToken


class ApprovalCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    reviewer_id: UUID
    channel: Literal["api", "web", "teams"] = "api"
    token: GrantToken = Field(repr=False)


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "edited", "rejected", "escalated"]
    edited_payload: dict[str, JsonValue] | None = None
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check_edited_payload(self) -> "ApprovalDecisionInput":
        if self.decision == "edited" and not self.edited_payload:
            raise ValueError("edited decision requires edited_payload")
        if self.decision != "edited" and self.edited_payload is not None:
            raise ValueError("edited_payload is only valid for edited decisions")
        return self


def _map_decision_error(error: Error, decision: str) -> HTTPException:
    message = (error.diag.message_primary or "").lower()
    if "approval request not found" in message:
        return HTTPException(404, "Approval request not found")
    if "cannot satisfy required role" in message and decision == "approved":
        return HTTPException(409, "Reviewer cannot satisfy the approval role")
    if "reviewer" in message or "cannot satisfy required role" in message:
        return HTTPException(403, "Reviewer is not authorized")
    if "matching current eligibility evidence" in message and decision == "edited":
        return HTTPException(
            409, "Order refund amount cannot be edited; amount is fixed by eligibility evidence"
        )
    if "past its sla" in message:
        return HTTPException(409, "Approval request is past its SLA")
    if "already " in message:
        return HTTPException(409, "Approval request is already resolved")
    if "valid amount or currency" in message or "validated subject or body" in message:
        return HTTPException(422, "Invalid edited payload")
    return HTTPException(409, "Approval decision could not be recorded")


def approval_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter(prefix="/approvals")
    try:
        credentials = TypeAdapter(list[ApprovalCredential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid approval credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Approval credentials must be unique")

    @router.post("/{request_id}/decide")
    def decide(
        request_id: UUID,
        body: ApprovalDecisionInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        credential = next(
            (entry for entry in credentials if compare_digest(token, entry.token)), None
        )
        if credential is None:
            raise HTTPException(403, "Approval credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                set_scope(conn, credential.tenant_id)
                row = conn.execute(
                    "SELECT public.record_approval_decision(%s, %s, %s, %s, %s, %s)",
                    (
                        request_id,
                        credential.reviewer_id,
                        body.decision,
                        credential.channel,
                        Jsonb(body.edited_payload) if body.edited_payload is not None else None,
                        body.comment,
                    ),
                ).fetchone()
                assert row is not None
                decision_id = row[0]
                return {
                    "decision_id": decision_id,
                    "request_id": request_id,
                    "decision": body.decision,
                }
        except InsufficientPrivilege:
            raise HTTPException(403, "Reviewer is not authorized") from None
        except DataError:
            raise HTTPException(422, "Invalid approval decision") from None
        except (IntegrityError, RaiseException) as error:
            raise _map_decision_error(error, body.decision) from None
        except (OperationalError, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Approval unavailable") from None
        except HTTPException:
            raise
        except Error:
            raise HTTPException(500, "Approval failed") from None

    return router
