from collections.abc import Callable
from decimal import Decimal
from hmac import compare_digest
from re import fullmatch
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import TypeAdapter, ValidationError

from app.adapters.ingest import audit
from app.ai_accounting import set_scope
from app.api.decide import DecideInput, publish
from app.api.lookups import LookupInput
from app.api.understand import Credential


def propose_refund(conn: Connection[Any], tenant: UUID, request: LookupInput) -> dict[str, Any]:
    set_scope(conn, tenant)
    scope = (tenant, request.case_id, request.message_id)
    row = conn.execute(
        """
        SELECT m.trace_id,c.pack_id,p.definition,tp.config
        FROM public.cases c
        JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
        JOIN public.packs p ON p.id=c.pack_id
        JOIN LATERAL (
          SELECT id,trace_id FROM public.messages
          WHERE (tenant_id,case_id)=(c.tenant_id,c.id) AND direction='inbound'
          ORDER BY created_at DESC,id DESC LIMIT 1
        ) m ON m.id=%s
        WHERE (c.tenant_id,c.id)=(%s,%s) AND tp.enabled
        FOR UPDATE OF c
        """,
        (request.message_id, tenant, request.case_id),
    ).fetchone()
    if row is None:
        raise HTTPException(409, "Refund proposal requires the latest inbound message")
    trace, pack_id, pack, enrollment = row
    config = pack.get("refund")
    if not config:
        return {"waiting": "refund_not_configured"}
    tool = config["tool"]
    available = conn.execute(
        """
        SELECT 1 FROM public.pack_tools pt JOIN public.tools t ON t.key=pt.tool_key
        WHERE pt.pack_id=%s AND t.key=%s AND t.active AND t.risk_level='money'
          AND public.is_order_scoped_tool(t) AND pt.approval_role IS NOT NULL
        """,
        (pack_id, tool),
    ).fetchone()
    if not available or enrollment.get("adapters", {}).get(tool, "mock") != "mock":
        return {"waiting": "refund_tool_unavailable"}

    understood = conn.execute(
        """
        SELECT cl.intent_key,cl.confidence,e.data->>'order_number',e.field_confidence->'order_number',
               e.status
        FROM LATERAL (
          SELECT intent_key,confidence,created_at FROM public.classifications
          WHERE (tenant_id,case_id,message_id)=(%s,%s,%s) AND pack_id=%s
          ORDER BY created_at DESC,id DESC LIMIT 1
        ) cl
        LEFT JOIN LATERAL (
          SELECT data,field_confidence,status FROM public.extractions
          WHERE (tenant_id,case_id,message_id)=(%s,%s,%s) AND pack_id=%s
            AND created_at >= cl.created_at
          ORDER BY created_at DESC,id DESC LIMIT 1
        ) e ON true
        """,
        (*scope, pack_id, *scope, pack_id),
    ).fetchone()
    if understood is None or understood[0] != config["intent"]:
        return {"waiting": "refund_intent_required"}
    _, confidence, order, field_confidence, extraction_status = understood
    thresholds = pack["thresholds"]
    if (
        confidence is None
        or extraction_status != "validated"
        or float(confidence) < thresholds["auto_confidence"]
        or not order
        or not isinstance(field_confidence, (int, float))
        or field_confidence < thresholds["field_confidence"]
    ):
        return {"waiting": "validated_order_required"}

    key = f"order-money:{request.message_id}:{order}"
    previous = conn.execute(
        """
        SELECT a.id,r.id,a.status FROM public.actions a
        JOIN public.approval_requests r
          ON (r.tenant_id,r.case_id,r.subject_type,r.subject_id)=(a.tenant_id,a.case_id,'action',a.id)
        WHERE a.tenant_id=%s AND a.idempotency_key=%s
        """,
        (tenant, key),
    ).fetchone()
    if previous:
        return {"action_id": previous[0], "approval_id": previous[1], "status": previous[2]}

    conn.execute("SELECT public.lock_order_evidence(%s,%s)", (tenant, order))
    existing = conn.execute(
        """
        SELECT 1 FROM public.actions WHERE tenant_id=%s AND input->>'order_number'=%s
          AND risk_level='money' AND status NOT IN ('rejected','cancelled','failed')
        """,
        (tenant, order),
    ).fetchone()
    if existing:
        return {"waiting": "order_refund_already_exists"}
    result = conn.execute(
        "SELECT public.order_eligibility(%s,%s,%s,%s)", (*scope, order)
    ).fetchone()
    evidence = result[0] if result else None
    if not isinstance(evidence, dict):
        return {"waiting": "current_eligibility_required"}
    if evidence.get("eligible") is not True:
        return {"waiting": evidence.get("reason") or "order_ineligible"}
    amount, currency = evidence.get("refundable_amount"), evidence.get("currency")
    if (
        not isinstance(amount, str)
        or not fullmatch(r"[0-9]{1,10}\.[0-9]{2}", amount)
        or Decimal(amount) <= 0
        or not isinstance(currency, str)
        or not fullmatch(r"[A-Z]{3}", currency)
    ):
        return {"waiting": "invalid_eligibility"}

    decision = publish(conn, tenant, DecideInput(**request.model_dump(), proposed_tools=[tool]))
    policy = conn.execute(
        "SELECT inputs->'budget_blocked' FROM public.policy_decisions WHERE tenant_id=%s AND id=%s",
        (tenant, decision["decision_id"]),
    ).fetchone()
    if (
        decision["decision"] != "needs_approval"
        or decision["rule_id"] != "G3"
        or policy != (False,)
    ):
        return {"waiting": "refund_policy_blocked"}
    conn.execute(
        """UPDATE public.cases c SET queue_id=q.id FROM public.queues q
           WHERE (c.tenant_id,c.id)=(%s,%s) AND c.queue_id IS NULL
             AND q.tenant_id=c.tenant_id AND q.key=%s""",
        (tenant, request.case_id, pack["default_queue"]),
    )
    action = conn.execute(
        """
        INSERT INTO public.actions (tenant_id,case_id,message_id,trace_id,policy_decision_id,
          idempotency_key,tool_key,risk_level,status,amount,currency,input)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'money','pending_approval',%s::numeric,%s,
                jsonb_build_object('order_number',%s::text,'amount',%s::text,'currency',%s::text))
        RETURNING id
        """,
        (
            *scope,
            trace,
            decision["decision_id"],
            key,
            tool,
            amount,
            currency,
            order,
            amount,
            currency,
        ),
    ).fetchone()
    assert action is not None
    approval = conn.execute(
        """
        INSERT INTO public.approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
        VALUES (%s,%s,'action',%s,'Review eligible order refund') RETURNING id
        """,
        (tenant, request.case_id, action[0]),
    ).fetchone()
    assert approval is not None
    audit(conn, tenant, trace, "action.proposed", "action", action[0])
    return {"action_id": action[0], "approval_id": approval[0], "status": "pending_approval"}


def refund_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid refund credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Refund credentials must be unique")

    @router.post("/refunds/propose")
    def endpoint(
        payload: LookupInput, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Refund credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                return propose_refund(conn, tenant, payload)
        except UniqueViolation as error:
            if error.diag.constraint_name == "actions_one_live_order_money":
                return {"waiting": "order_refund_already_exists"}
            raise HTTPException(409, "Refund proposal conflicted") from None
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Refund proposal unavailable") from None

    return router
