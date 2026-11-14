from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from secrets import token_hex
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from jsonschema import Draft202012Validator
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
from referencing import Registry

GrantToken = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\x00]+$")]


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    role: Literal["worker", "reconciler", "approval_poster"]
    principal: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    token: GrantToken = Field(repr=False)


class Consume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: GrantToken
    adapter: Literal["mock", "real"] = "mock"


class Complete(Consume):
    external_ref: Reference
    # Read tools return a result, validated against the tool's registry output schema.
    output: dict[str, JsonValue] | None = None


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


class SlackDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,64}$")]
    ts: Annotated[str, Field(pattern=r"^[0-9]{1,16}\.[0-9]{6}$")]


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

    def authenticate(
        role: Literal["worker", "reconciler", "approval_poster"],
    ) -> Callable[..., Credential]:
        def require(authorization: Annotated[str | None, Header()] = None) -> Credential:
            token = bearer_token(authorization)
            for entry in credentials:
                if entry.role == role and compare_digest(token, entry.token):
                    return entry
            raise HTTPException(403, "Execution credential is not authorized")

        return require

    worker = authenticate("worker")
    reconciler = authenticate("reconciler")
    poster = authenticate("approval_poster")

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
        if expires is None:
            expires = transition(
                credential,
                "SELECT public.replace_unused_mock_grant(%s,%s,%s)",
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
        if body.adapter != "mock":
            raise HTTPException(409, "Real dispatch is unavailable in the offline deployment")
        dispatch = transition(
            credential,
            "SELECT public.consume_mock_grant(%s,%s,%s)",
            (action_id, credential.principal, sha256(body.grant.encode("ascii")).digest()),
        )
        if dispatch is None:
            raise HTTPException(409, "Grant cannot be consumed; re-claim before dispatch")
        return dispatch

    @router.post("/{action_id}/complete")
    def complete(
        action_id: UUID,
        body: Complete,
        credential: Annotated[Credential, Depends(worker)],
    ) -> dict[str, str]:
        digest = sha256(body.grant.encode("ascii")).digest()
        if body.output is None:
            result = transition(
                credential,
                "SELECT public.complete_mock_action(%s,%s,%s,%s)",
                (action_id, credential.principal, digest, body.external_ref),
            )
        else:
            schema = transition(
                credential,
                """
                SELECT t.output_schema FROM public.actions a JOIN public.tools t ON t.key=a.tool_key
                WHERE a.tenant_id=%s AND a.id=%s AND a.risk_level='read'
                """,
                (credential.tenant_id, action_id),
            )
            if (
                not isinstance(schema, dict)
                or not schema
                or not Draft202012Validator(schema, registry=Registry()).is_valid(body.output)
            ):
                raise HTTPException(422, "Read result does not match the tool's output schema")
            result = transition(
                credential,
                "SELECT public.complete_mock_read(%s,%s,%s,%s,%s)",
                (action_id, credential.principal, digest, body.external_ref, Jsonb(body.output)),
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

    @router.get("/reconciliation/pending")
    def reconciliation_pending(
        credential: Annotated[Credential, Depends(reconciler)], tool_key: str = "send_reply"
    ) -> object:
        return transition(
            credential,
            """
            SELECT coalesce(jsonb_agg(to_jsonb(item)), '[]'::jsonb)
              FROM (SELECT id AS action_id, tenant_id, trace_id
                      FROM public.actions
                     WHERE tenant_id = %s AND adapter = 'mock'
                       AND tool_key = %s
                       AND (status = 'unknown_outcome' OR
                            (status = 'executing' AND lease_expires_at <= clock_timestamp()))
                     ORDER BY lease_expires_at, id LIMIT 100) item
            """,
            (credential.tenant_id, tool_key),
        )

    @router.get("/dispatch/{tool_key}")
    def dispatch_pending(
        tool_key: str, credential: Annotated[Credential, Depends(worker)]
    ) -> object:
        return transition(
            credential,
            """
            SELECT coalesce(jsonb_agg(jsonb_build_object('action_id', item.id)), '[]'::jsonb)
              FROM (SELECT id FROM public.actions a
                     WHERE tenant_id = %s AND adapter = 'mock' AND tool_key = %s
                       AND (status = 'approved' OR (status = 'proposed' AND EXISTS (
                         SELECT 1 FROM public.policy_decisions p JOIN public.cases c
                           ON (c.tenant_id,c.id,c.policy_revision)=(p.tenant_id,p.case_id,p.revision)
                         WHERE (p.tenant_id,p.case_id,p.id)=(a.tenant_id,a.case_id,a.policy_decision_id)
                           AND ((p.decision='auto' AND a.tool_key=ANY(p.allowed_tools))
                                OR public.has_current_mock_reply_review(a,c.policy_revision))
                       )) OR (status = 'executing' AND claimed_by = %s
                            AND lease_expires_at > clock_timestamp()))
                     ORDER BY created_at, id LIMIT 100) item
            """,
            (
                credential.tenant_id,
                "send_reply" if tool_key == "send-reply" else tool_key,
                credential.principal,
            ),
        )

    @router.get("/approvals/pending")
    def approvals_pending(credential: Annotated[Credential, Depends(poster)]) -> object:
        return transition(
            credential,
            """
            SELECT coalesce(jsonb_agg(jsonb_build_object('request_id', item.id)), '[]'::jsonb)
              FROM (SELECT r.id FROM public.approval_requests r
                      JOIN public.cases c ON (c.tenant_id,c.id) = (r.tenant_id,r.case_id)
                      JOIN public.queues q ON (q.tenant_id,q.id) = (c.tenant_id,c.queue_id)
                      JOIN public.tenant_settings s ON s.tenant_id = r.tenant_id
                     WHERE r.tenant_id = %s AND r.status = 'pending' AND r.slack_message_ts IS NULL
                       AND r.sla_due_at > clock_timestamp() AND q.notify_channel IS NOT NULL
                       AND s.slack_workspace_id IS NOT NULL
                       AND (r.subject_type = 'draft' OR (r.subject_type = 'action' AND EXISTS (
                         SELECT 1 FROM public.actions a WHERE (a.tenant_id,a.case_id,a.id) =
                           (r.tenant_id,r.case_id,r.subject_id) AND a.adapter = 'mock'
                       )))
                     ORDER BY r.created_at,r.id LIMIT 100) item
            """,
            (credential.tenant_id,),
        )

    @router.get("/approvals/{request_id}/notification")
    def approval_notification(
        request_id: UUID,
        credential: Annotated[Credential, Depends(poster)],
    ) -> object:
        details = transition(
            credential,
            """
            SELECT jsonb_build_object(
              'request_id', r.id, 'subject_type', r.subject_type, 'required_role', r.required_role,
              'channel', q.notify_channel, 'workspace', s.slack_workspace_id,
              'posted_ts', r.slack_message_ts, 'deadline', r.sla_due_at,
              'review', jsonb_strip_nulls(jsonb_build_object(
                'tool', a.tool_key, 'amount', a.amount, 'currency', a.currency,
                'recipient', d.recipient, 'subject', d.subject, 'body', d.body
              ))
            )
            FROM public.approval_requests r
            JOIN public.cases c ON (c.tenant_id,c.id) = (r.tenant_id,r.case_id)
            JOIN public.queues q ON (q.tenant_id,q.id) = (c.tenant_id,c.queue_id)
            JOIN public.tenant_settings s ON s.tenant_id = c.tenant_id
            LEFT JOIN public.actions a ON r.subject_type = 'action'
              AND (a.tenant_id,a.case_id,a.id) = (r.tenant_id,r.case_id,r.subject_id)
            LEFT JOIN public.drafts d ON r.subject_type = 'draft'
              AND (d.tenant_id,d.case_id,d.id) = (r.tenant_id,r.case_id,r.subject_id)
            WHERE r.tenant_id = %s AND r.id = %s AND r.status = 'pending'
              AND (d.id IS NOT NULL OR a.adapter = 'mock')
              AND r.sla_due_at > clock_timestamp()
              AND q.notify_channel IS NOT NULL AND s.slack_workspace_id IS NOT NULL
            """,
            (credential.tenant_id, request_id),
        )
        if details is None:
            raise HTTPException(409, "Approval has no pending Slack notification")
        return details

    @router.post("/approvals/{request_id}/notification")
    def record_approval_notification(
        request_id: UUID,
        body: SlackDelivery,
        credential: Annotated[Credential, Depends(poster)],
    ) -> dict[str, str]:
        saved = transition(
            credential,
            """
            UPDATE public.approval_requests r
               SET slack_channel = %s, slack_message_ts = %s
              FROM public.cases c JOIN public.queues q
                ON (q.tenant_id,q.id) = (c.tenant_id,c.queue_id)
             WHERE r.tenant_id = %s AND r.id = %s AND r.status = 'pending'
               AND (r.tenant_id,r.case_id) = (c.tenant_id,c.id)
               AND q.notify_channel = %s AND r.sla_due_at > clock_timestamp()
               AND (r.slack_message_ts IS NULL OR (r.slack_channel,r.slack_message_ts) = (%s,%s))
             RETURNING r.id
            """,
            (
                body.channel,
                body.ts,
                credential.tenant_id,
                request_id,
                body.channel,
                body.channel,
                body.ts,
            ),
        )
        if saved is None:
            raise HTTPException(409, "Slack delivery could not be recorded")
        return {"status": "posted"}

    @router.post("/approvals/expire")
    def expire_approvals(credential: Annotated[Credential, Depends(poster)]) -> object:
        return transition(
            credential,
            """
            SELECT count(*) FROM (
                SELECT public.expire_approval_request(id) AS expired
                FROM public.approval_requests WHERE tenant_id=%s AND status='pending'
                  AND sla_due_at <= clock_timestamp()
                ORDER BY sla_due_at,id LIMIT 100
            ) batch WHERE expired
            """,
            (credential.tenant_id,),
        )

    return router
