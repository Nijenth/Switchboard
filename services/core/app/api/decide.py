from collections.abc import Callable
from dataclasses import asdict
from hmac import compare_digest
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.adapters.ingest import audit
from app.ai_accounting import set_scope
from app.api.queue_reply import queue_reply
from app.api.understand import Credential
from app.engine.policy import decide

ToolKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,60}$")]


class DecideInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    message_id: UUID
    proposed_tools: list[ToolKey] = Field(default_factory=list, max_length=32)
    queue_reply: bool = False


def publish(
    conn: Connection[Any],
    tenant: UUID,
    payload: DecideInput,
    *,
    draft_id: UUID | None = None,
) -> dict[str, Any]:
    set_scope(conn, tenant)
    scope = (tenant, payload.case_id)
    row = conn.execute(
        """
        SELECT c.pack_id,c.policy_revision,c.channel,c.status,c.priority,c.identity_verified,
               t.slug,p.key,p.version,p.definition,tp.config,
               co.id,co.kind,co.locale
        FROM public.cases c
        JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
        JOIN public.packs p ON p.id=c.pack_id
        JOIN public.tenants t ON t.id=c.tenant_id
        LEFT JOIN public.contacts co ON (co.tenant_id,co.id)=(c.tenant_id,c.contact_id)
        WHERE c.tenant_id=%s AND c.id=%s AND tp.enabled
        FOR UPDATE OF c
        """,
        scope,
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Decision case unavailable")
    (
        pack_id,
        revision,
        channel,
        status,
        priority,
        verified,
        slug,
        pack_key,
        version,
        pack,
        enrollment,
        contact_id,
        contact_kind,
        contact_locale,
    ) = row
    latest = conn.execute(
        """
        SELECT id,trace_id FROM public.messages
        WHERE tenant_id=%s AND case_id=%s AND direction='inbound'
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        scope,
    ).fetchone()
    if latest is None or latest[0] != payload.message_id:
        raise HTTPException(409, "Decision requires the latest inbound message")
    trace = latest[1]
    message_scope = (*scope, payload.message_id, pack_id)
    classification = conn.execute(
        """
        SELECT id,intent_key,confidence,priority,language,sentiment,flags,created_at
        FROM public.classifications
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND pack_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope,
    ).fetchone()
    flags = conn.execute(
        """
        SELECT DISTINCT f FROM public.classifications c CROSS JOIN LATERAL unnest(c.flags) AS f
        WHERE c.tenant_id=%s AND c.case_id=%s ORDER BY f
        """,
        scope,
    ).fetchall()
    extracted = conn.execute(
        """
        SELECT id,data,field_confidence,status,created_at FROM public.extractions
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND pack_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope,
    ).fetchone()
    budget = conn.execute(
        """
        SELECT s.monthly_ai_budget_usd <= COALESCE((
            SELECT sum(CASE WHEN r.accounting_pending THEN r.reserved_cost_usd ELSE r.cost_usd END)
            FROM public.ai_runs r WHERE r.tenant_id=s.tenant_id
              AND r.created_at >= date_trunc('month', statement_timestamp(), 'UTC')
        ),0) FROM public.tenant_settings s WHERE s.tenant_id=%s
        """,
        (tenant,),
    ).fetchone()
    blocked_attempt = conn.execute(
        """
        SELECT status,step,created_at FROM public.ai_runs
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope[:3],
    ).fetchone()
    registry_tools = conn.execute(
        """
        SELECT t.key,t.risk_level,t.requires_verified_identity,pt.approval_role,t.output_schema,
               public.is_identity_check_tool(t),public.is_order_scoped_tool(t)
        FROM public.pack_tools pt JOIN public.tools t ON t.key=pt.tool_key
        WHERE pt.pack_id=%s
        """,
        (pack_id,),
    ).fetchall()
    tools = {
        key: {
            "risk_level": risk,
            "requires_verified_identity": identity,
            "approval_role": role,
            "check_fields": list(schema.get("properties", {})),
        }
        for key, risk, identity, role, schema, _, _ in registry_tools
    }
    proposed = set(payload.proposed_tools)
    if proposed - tools.keys():
        raise HTTPException(422, "Tool is outside the pinned pack")
    labels = ("id", "intent_key", "confidence", "priority", "language", "sentiment", "flags")
    label = dict(zip(labels, classification[:-1], strict=True)) if classification else {}
    if classification:
        label["id"] = str(label["id"])
        label["confidence"] = float(label["confidence"])
        if (
            blocked_attempt
            and blocked_attempt[1] == "classify"
            and blocked_attempt[0] != "success"
            and blocked_attempt[2] >= classification[-1]
        ):
            label["confidence"] = None
    label["flags"] = [flag for (flag,) in flags]
    # Results of completed read lookups for this message become checks.* (ADR 0019, ADR 0021).
    checks: dict[str, Any] = {}
    for (output,) in conn.execute(
        """
        SELECT a.output FROM public.actions a JOIN public.tools t ON t.key=a.tool_key AND t.active
        WHERE a.tenant_id=%s AND a.case_id=%s AND a.message_id=%s AND a.risk_level='read'
          AND a.status='executed' AND jsonb_typeof(a.output)='object'
          AND public.tool_identity_authorized(a,t)
          AND (NOT t.requires_verified_identity OR public.valid_tool_output(t.output_schema,a.output))
        ORDER BY a.executed_at, a.id
        """,
        message_scope[:3],
    ):
        checks.update(output)
    extraction_data = extracted[1] if extracted and isinstance(extracted[1], dict) else {}
    order_number = extraction_data.get("order_number")
    if any(identity_check or order_scoped for *_, identity_check, order_scoped in registry_tools):
        verified = bool(
            verified
            and isinstance(order_number, str)
            and conn.execute(
                "SELECT public.order_identity_is_verified(%s,%s,%s,%s)",
                (tenant, payload.case_id, payload.message_id, order_number),
            ).fetchone()
            == (True,)
        )
    if any(
        identity and not order_scoped and key in proposed
        for key, _, identity, _, _, _, order_scoped in registry_tools
    ):
        verified = False
    context = {
        "case": {
            "id": str(payload.case_id),
            "channel": channel,
            "status": status,
            "priority": priority,
            "identity_verified": verified,
        },
        "tenant": {"id": str(tenant), "slug": slug},
        "contact": {"id": str(contact_id), "kind": contact_kind, "locale": contact_locale}
        if contact_id
        else {},
        "classification": label,
        "extraction": extraction_data,
        "field_confidence": extracted[2] if extracted else {},
        "extraction_valid": (
            extracted is not None
            and classification is not None
            and extracted[3] == "validated"
            and extracted[4] >= classification[-1]
        ),
        "checks": checks,
    }
    budget_blocked = (
        budget is None
        or budget[0]
        or (blocked_attempt is not None and blocked_attempt[0] == "budget_blocked")
    )
    overrides = enrollment.get("rules", []) if isinstance(enrollment, dict) else None
    draft = None
    if draft_id is not None:
        draft = conn.execute(
            """SELECT payload_hash,guardrail_results FROM public.drafts
               WHERE tenant_id=%s AND case_id=%s AND in_reply_to_message_id=%s
                 AND id=%s AND status='pending' FOR UPDATE""",
            (tenant, payload.case_id, payload.message_id, draft_id),
        ).fetchone()
        if draft is None:
            raise HTTPException(409, "Draft unavailable for policy evaluation")
    citations_ok = bool(draft and draft[1].get("g6_passed") is True)
    output_ok = bool(draft and draft[1].get("g7_passed") is True)
    decision = decide(
        pack,
        context,
        tools,
        proposed,
        overrides,
        citations_ok=citations_ok,
        output_ok=output_ok,
        budget_blocked=budget_blocked,
    )
    result = {**asdict(decision), "allowed_tools": list(decision.allowed_tools)}
    inputs = {
        **context,
        "extraction_id": str(extracted[0]) if extracted else None,
        "proposed_tools": sorted(proposed),
        "tool_policy": tools,
        "budget_blocked": budget_blocked,
        "citations_verified": citations_ok,
        "output_guardrails_passed": output_ok,
        "draft_id": str(draft_id) if draft_id else None,
        "draft_payload_hash": draft[0] if draft else None,
        "tenant_rules": overrides,
        "result": result,
    }
    policy_version = f"engine:2+{pack_key}:{version}"
    previous = conn.execute(
        """
        SELECT id,inputs FROM public.policy_decisions
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND revision=%s AND policy_version=%s
        """,
        (*message_scope[:3], revision, policy_version),
    ).fetchone()
    if previous is not None and previous[1] == inputs:
        return {"decision_id": previous[0], "revision": revision, "trace_id": trace, **result}
    published = conn.execute(
        """
        INSERT INTO public.policy_decisions (
            tenant_id,case_id,message_id,pack_id,trace_id,decision,rule_id,rule_source,
            reason,required_role,allowed_tools,policy_version,inputs,draft_id,draft_payload_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,revision
        """,
        (
            *message_scope,
            trace,
            decision.decision,
            decision.rule_id,
            decision.rule_source,
            decision.reason,
            decision.required_role,
            list(decision.allowed_tools),
            policy_version,
            Jsonb(inputs),
            draft_id,
            draft[0] if draft else None,
        ),
    ).fetchone()
    assert published is not None
    decision_id, revision = published
    audit(conn, tenant, trace, "policy.decided", "policy_decision", decision_id)
    return {"decision_id": decision_id, "revision": revision, "trace_id": trace, **result}


def decide_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid decision credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Decision credentials must be unique")

    @router.post("/decide")
    def endpoint(
        payload: DecideInput, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Decision credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                if payload.queue_reply:
                    set_scope(conn, tenant)
                    return queue_reply(conn, tenant, payload.case_id, payload.message_id)
                return publish(conn, tenant, payload)
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Decision unavailable") from None

    return router
